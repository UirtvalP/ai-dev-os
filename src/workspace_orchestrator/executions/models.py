"""Task、Execution 与 Agent Session 分离后的领域模型。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

ExecutionStatus = Literal[
    "queued", "starting", "running", "waiting", "blocked",
    "completed", "failed", "cancelled",
]


@dataclass(frozen=True, slots=True)
class Execution:
    id: str
    requirement_id: str
    task_id: str
    role: str
    runtime_id: str
    provider: str
    model: str | None
    reasoning_effort: str | None
    workspace_path: str
    status: ExecutionStatus
    created_at: str
    updated_at: str
    prompt: str
    session_id: str | None = None
    turn_id: str | None = None
    branch: str | None = None
    worktree: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    last_progress_at: str | None = None
    parent_execution_id: str | None = None
    execution_policy: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    event_cursor: int = 0
    source: str = "workbench"
    creation_key: str | None = None
    schema_version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(self.extra)
        result.update(asdict(self))
        result.pop("extra", None)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Execution:
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError("不支持 Execution schema_version")
        required = (
            "id", "requirement_id", "task_id", "role", "runtime_id", "provider",
            "workspace_path", "status", "created_at", "updated_at", "prompt",
        )
        for name in required:
            if not isinstance(value.get(name), str) or not str(value[name]).strip():
                raise ValueError(f"Execution {name} 必须是非空字符串")
        if value["status"] not in {
            "queued", "starting", "running", "waiting", "blocked",
            "completed", "failed", "cancelled",
        }:
            raise ValueError("Execution status 不合法")
        for name in (
            "model", "reasoning_effort", "session_id", "turn_id", "branch", "worktree",
            "started_at", "completed_at", "last_progress_at", "parent_execution_id",
            "creation_key",
        ):
            item = value.get(name)
            if item is not None and (not isinstance(item, str) or not item.strip()):
                raise ValueError(f"Execution {name} 必须是非空字符串或 null")
        if type(value.get("event_cursor", 0)) is not int or value.get("event_cursor", 0) < 0:
            raise ValueError("Execution event_cursor 必须是非负整数")
        for name in ("execution_policy", "result"):
            if not isinstance(value.get(name, {}), dict):
                raise TypeError(f"Execution {name} 必须是 JSON 对象")
        if value.get("error") is not None and not isinstance(value["error"], dict):
            raise TypeError("Execution error 必须是 JSON 对象或 null")
        known = {item.name for item in cls.__dataclass_fields__.values()} - {"extra"}
        payload = {name: copy.deepcopy(value.get(name)) for name in known if name in value}
        payload["extra"] = copy.deepcopy({key: item for key, item in value.items() if key not in known})
        return cls(**cast(Any, payload))
