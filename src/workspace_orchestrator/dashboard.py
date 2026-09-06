"""Phase 5 的本地只读投影与持久指令队列。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
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
        command = DashboardCommand(command_id or f"cmd-{uuid4().hex}", requirement_id,
                                   session_id, message)
        with _file_lock(self.path.with_suffix(".lock")):
            rows = self._read()
            previous = next((DashboardCommand(**row) for row in rows
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
        return tuple(DashboardCommand(**row) for row in self._read()
                     if row["session_id"] == session_id and row["status"] == "queued")

    def history(
        self, *, requirement_id: str | None = None, session_id: str | None = None,
    ) -> tuple[DashboardCommand, ...]:
        """按固定控制范围读取队列历史，不让 Dashboard 成为另一套事实源。"""

        return tuple(
            DashboardCommand(**row) for row in self._read()
            if (requirement_id is None or row["requirement_id"] == requirement_id)
            and (session_id is None or row["session_id"] == session_id)
        )

    def update(self, command_id: str, status: CommandStatus, result: str = "") -> DashboardCommand:
        if status not in {"delivered", "completed", "failed", "cancelled"}:
            raise WorkspaceError("指令状态无效")
        with _file_lock(self.path.with_suffix(".lock")):
            rows = self._read()
            for index, row in enumerate(rows):
                if row["command_id"] == command_id:
                    row = {**row, "status": status, "result": result}
                    rows[index] = row
                    self._write(rows)
                    return DashboardCommand(**row)
        raise WorkspaceError("找不到指令")

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise WorkspaceError("Dashboard 指令队列损坏")
        return value

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


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
