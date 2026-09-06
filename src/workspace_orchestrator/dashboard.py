"""Phase 5 的本地只读投影与持久指令队列。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

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
            "events": [event.to_dict() for event in event_rows],
            "next_cursor": event_rows[-1].sequence if event_rows else after,
        }
