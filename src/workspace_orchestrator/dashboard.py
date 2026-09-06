"""Phase 5 的本地只读投影与持久指令队列。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .adapters.git import GitError, LocalGitProvider
from .agent_runtime.events import RuntimeEventStore
from .workspace import WorkspaceError, WorkspaceStore, _file_lock

CommandStatus = Literal["queued", "delivered", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class DashboardCommand:
    command_id: str
    requirement_id: str
    session_id: str
    message: str
    status: CommandStatus = "queued"
    result: str = ""
    created_at: str = ""
    delivered_at: str | None = None
    completed_at: str | None = None
    attempt: int = 1
    retry_of: str | None = None


class CommandQueue:
    """小型 JSON 队列；command_id 让客户端重试不会重复投递。"""

    def __init__(self, path: Path, *, max_message_length: int = 16_384) -> None:
        self.path = path
        self.max_message_length = max_message_length

    def enqueue(self, requirement_id: str, session_id: str, message: str,
                *, command_id: str | None = None) -> DashboardCommand:
        if not all(isinstance(v, str) and v.strip() for v in (requirement_id, session_id, message)):
            raise WorkspaceError("Requirement、Session 和指令不能为空")
        if len(message) > self.max_message_length:
            raise WorkspaceError("指令超过长度限制")
        identifier = command_id or f"cmd-{uuid4().hex}"
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identifier) is None:
            raise WorkspaceError("command_id 必须是 1 至 128 位安全标识符")
        command = DashboardCommand(
            identifier, requirement_id, session_id, message, created_at=_now(),
        )
        with _file_lock(self.path.with_suffix(".lock")):
            rows = self._read()
            previous = next((_command(row) for row in rows
                             if row["command_id"] == command.command_id), None)
            if previous:
                if previous.requirement_id != requirement_id or previous.session_id != session_id \
                        or previous.message != message:
                    raise WorkspaceError("command_id 已存在但内容不同")
                return previous
            rows.append(asdict(command))
            self._write(rows)
        return command

    def pending(self, session_id: str) -> tuple[DashboardCommand, ...]:
        return tuple(_command(row) for row in self._read()
                     if row["session_id"] == session_id and row["status"] == "queued")

    def history(
        self, *, requirement_id: str | None = None, session_id: str | None = None,
    ) -> tuple[DashboardCommand, ...]:
        """按固定控制范围读取队列历史，不让 Dashboard 成为另一套事实源。"""

        return tuple(
            _command(row) for row in self._read()
            if (requirement_id is None or row["requirement_id"] == requirement_id)
            and (session_id is None or row["session_id"] == session_id)
        )

    def recover_delivered(self, requirement_id: str) -> tuple[DashboardCommand, ...]:
        """重启时把结果未知的已投递指令转为显式失败，禁止静默遗失或重复执行。"""

        recovered: list[DashboardCommand] = []
        for command in self.history(requirement_id=requirement_id):
            if command.status == "delivered":
                recovered.append(self.update(
                    command.command_id,
                    "failed",
                    "控制服务重启，投递结果未知；请核对 Agent 状态后显式重试",
                ))
        return tuple(recovered)

    def update(self, command_id: str, status: CommandStatus, result: str = "") -> DashboardCommand:
        if status not in {"delivered", "completed", "failed", "cancelled"}:
            raise WorkspaceError("指令状态无效")
        with _file_lock(self.path.with_suffix(".lock")):
            rows = self._read()
            for index, row in enumerate(rows):
                if row["command_id"] == command_id:
                    current = _command(row)
                    if current.status in {"completed", "failed", "cancelled"}:
                        return current
                    timestamp = _now()
                    row = {
                        **asdict(current),
                        "status": status,
                        "result": result,
                        "delivered_at": current.delivered_at or (
                            timestamp if status == "delivered" else None
                        ),
                        "completed_at": timestamp if status in {
                            "completed", "failed", "cancelled"
                        } else None,
                    }
                    rows[index] = row
                    self._write(rows)
                    return _command(row)
        raise WorkspaceError("找不到指令")

    def cancel(self, command_id: str) -> DashboardCommand:
        """仅队列中的指令可直接取消；已投递指令由 RuntimeController 中断。"""

        with _file_lock(self.path.with_suffix(".lock")):
            rows = self._read()
            for index, row in enumerate(rows):
                if row["command_id"] != command_id:
                    continue
                command = _command(row)
                if command.status != "queued":
                    return command
                command = DashboardCommand(
                    **{
                        **asdict(command),
                        "status": "cancelled",
                        "result": "用户在投递前取消",
                        "completed_at": _now(),
                    }
                )
                rows[index] = asdict(command)
                self._write(rows)
                return command
        raise WorkspaceError("找不到指令")

    def retry(self, command_id: str, *, retry_id: str) -> DashboardCommand:
        """为失败指令创建一次显式重试；retry_id 保证浏览器重放不重复发送。"""

        rows = self._read()
        previous = next((_command(row) for row in rows if row["command_id"] == command_id), None)
        if previous is None:
            raise WorkspaceError("找不到指令")
        if previous.status != "failed":
            raise WorkspaceError("只有失败指令可以重试")
        retried = self.enqueue(
            previous.requirement_id,
            previous.session_id,
            previous.message,
            command_id=retry_id,
        )
        if retried.retry_of is not None:
            return retried
        with _file_lock(self.path.with_suffix(".lock")):
            current = self._read()
            for index, row in enumerate(current):
                if row["command_id"] == retry_id:
                    row = {
                        **asdict(_command(row)),
                        "attempt": previous.attempt + 1,
                        "retry_of": previous.command_id,
                    }
                    current[index] = row
                    self._write(current)
                    return _command(row)
        raise WorkspaceError("重试指令持久化失败")

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise WorkspaceError("Dashboard 指令队列损坏")
        return [asdict(_command(row)) for row in value]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _command(row: dict[str, Any]) -> DashboardCommand:
    """兼容 Phase 5 MVP 已持久化的无时间戳队列记录。"""

    fields = DashboardCommand.__dataclass_fields__
    return DashboardCommand(**{key: value for key, value in row.items() if key in fields})


class DashboardService:
    """从既有事实源组装页面数据，不创建第二套状态。"""

    def __init__(self, workspace: WorkspaceStore, events: RuntimeEventStore,
                 commands: CommandQueue) -> None:
        self.workspace, self.events, self.commands = workspace, events, commands

    def requirement(self, requirement_id: str, *, run_id: str | None = None,
                    after: int = 0, limit: int = 200) -> dict[str, Any]:
        snapshot = self.workspace.load(requirement_id)
        event_rows = () if run_id is None else self.events.replay(run_id, after=after, limit=limit)
        return {
            "requirement_id": requirement_id,
            "workspace": snapshot,
            "projection": {
                "phases": self._phases(requirement_id),
                "agents": self._agents(snapshot),
                "verification": self._verification(requirement_id),
                "git": self._git(),
                "blockers": _markdown_list(snapshot["state"], "已阻塞"),
                "next_actions": _markdown_list(snapshot["state"], "下一步行动"),
            },
            "events": [event.to_dict() for event in event_rows],
            "next_cursor": event_rows[-1].sequence if event_rows else after,
        }

    def _phases(self, requirement_id: str) -> list[dict[str, Any]]:
        definitions = (
            self.workspace.working_root / ".ai-dev-os" / "gate-definitions" / requirement_id
        )
        workspace = self.workspace.path_for(requirement_id)
        phases: list[dict[str, Any]] = []
        for path in sorted(definitions.glob("phase-*.json")):
            definition = _read_object(path)
            phase = definition.get("phase")
            if not isinstance(phase, int):
                continue
            gate = _read_object(workspace / "phase-gates" / f"phase-{phase}.json")
            activation = _read_object(
                workspace / "phase-activations" / f"phase-{phase}.json"
            )
            status = "passed" if gate.get("status") == "PASS" else (
                "active" if activation else "blocked"
            )
            results = {
                item.get("acceptance_id"): item.get("status")
                for item in gate.get("acceptance_results", [])
                if isinstance(item, dict)
            }
            acceptance = [
                {
                    **item,
                    "status": results.get(item.get("id"), "PASS" if status == "passed" else "TODO"),
                }
                for item in definition.get("acceptance", [])
                if isinstance(item, dict)
            ]
            phases.append({
                "phase": phase,
                "task_id": definition.get("task_id"),
                "title": _phase_title(path, phase),
                "status": status,
                "commit_sha": gate.get("commit_sha"),
                "activated_at": activation.get("activated_at"),
                "acceptance": acceptance,
                "passed_acceptance": sum(item["status"] == "PASS" for item in acceptance),
                "total_acceptance": len(acceptance),
            })
        return phases

    def _agents(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        current_task = snapshot["meta"].get("requirement_task_id")
        result = []
        for session in snapshot["sessions"]:
            task_ids = session.get("task_ids") or []
            active = session.get("result") == "in_progress"
            role = "Primary" if current_task in task_ids else (
                "Control" if active and not task_ids else "Workspace Agent"
            )
            result.append({
                "session_id": session.get("id"),
                "runtime": session.get("agent") or "unknown",
                "role": role,
                "status": "active" if active else session.get("result", "unknown"),
                "task_ids": task_ids,
                "started_at": session.get("started_at"),
                "ended_at": session.get("ended_at"),
                "branch": session.get("branch"),
                "worktree": session.get("worktree"),
            })
        return sorted(result, key=lambda item: (item["status"] != "active", item["started_at"] or ""))

    def _verification(self, requirement_id: str) -> list[dict[str, Any]]:
        root = self.workspace.path_for(requirement_id) / "verification-receipts"
        receipts = []
        for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            item = _read_object(path)
            if not item:
                continue
            receipts.append({
                "receipt_id": item.get("receipt_id") or path.stem,
                "suite_id": item.get("suite_id"),
                "status": item.get("status"),
                "commit_sha": item.get("commit_sha"),
                "summary": str(item.get("summary") or "")[:500],
                "completed_at": item.get("completed_at"),
                "source_url": item.get("source_url"),
            })
            if len(receipts) == 12:
                break
        return receipts

    def _git(self) -> dict[str, Any]:
        try:
            return LocalGitProvider(self.workspace.working_root).push_status()
        except GitError as exc:
            return {"error": str(exc)}


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _phase_title(definition_path: Path, phase: int) -> str:
    definition = _read_object(definition_path)
    relative = definition.get("plan_source_path")
    if isinstance(relative, str):
        plan = definition_path.parents[3] / relative
        if plan.is_file():
            for line in plan.read_text(encoding="utf-8").splitlines():
                heading = re.match(r"^#{1,3}\s+(?:(\d+)\.\s*)?(.*)$", line)
                if not heading:
                    continue
                section_phase = heading.group(1) == str(phase)
                named_phase = bool(re.search(rf"\bPhase\s+{phase}\b", heading.group(2)))
                if section_phase or named_phase:
                    title = re.sub(r"[（(]AID-\d+[）)]", "", heading.group(2)).strip()
                    return title
    return f"Phase {phase}"


def _markdown_list(markdown: str, heading: str) -> list[str]:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.MULTILINE,
    )
    if not match:
        return []
    items = [line[2:].strip() for line in match.group(1).splitlines() if line.startswith("- ")]
    if items:
        return [] if all(item.startswith("无") for item in items) else items
    text = match.group(1).strip()
    return [] if not text or text == "无" else [text]
