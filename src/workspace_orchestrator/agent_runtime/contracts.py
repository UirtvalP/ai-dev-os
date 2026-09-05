"""Runtime 的版本化数据契约；不包含产品 API 或 Requirement 状态转换。"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1
OperationStatus = Literal["ok", "unsupported", "unavailable", "failed", "timeout"]


def event_timestamp() -> str:
    """生成带时区的事件时间；排序依据是持久序号而不是墙钟。"""

    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class RuntimeFailure:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    id: str
    name: str
    reasoning_efforts: tuple[str, ...] = ()
    is_default: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    runtime_id: str
    display_name: str
    version: str
    available: bool
    capabilities: tuple[str, ...] = ()
    models: tuple[ModelDescriptor, ...] = ()
    reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def supports(self, capability: str) -> bool:
        return self.available and capability in self.capabilities


@dataclass(frozen=True, slots=True)
class RuntimeSessionRef:
    runtime_id: str
    session_id: str
    run_id: str = ""
    workspace_path: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    run_id: str
    workspace_path: Path
    prompt: str
    sandbox: str = "workspace-write"
    model: str | None = None
    resume_session_id: str | None = None
    bypass_hook_trust: bool = False
    timeout_seconds: float = 7200
    requirement_id: str | None = None
    task_id: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """进程/轮次结果，不构成 Task 或 Requirement 完成授权。

    前五个字段保持原 CodexExecutionResult 的构造和读取兼容。
    """

    returncode: int
    session_id: str | None
    stdout: str
    stderr: str
    resumed: bool = False
    runtime_id: str = "legacy"
    run_id: str | None = None
    summary: str = ""
    error: RuntimeFailure | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class RuntimeOperationResult:
    status: OperationStatus
    session: RuntimeSessionRef | None = None
    turn_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: RuntimeFailure | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """原始 Provider 数据保存在 payload，未知顶层扩展亦保真。"""

    event_id: str
    run_id: str
    runtime_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str | None = None
    timestamp: str = field(default_factory=event_timestamp)
    sequence: int = 0
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(self.extra)
        result.update(
            event_id=self.event_id,
            run_id=self.run_id,
            runtime_id=self.runtime_id,
            kind=self.kind,
            payload=copy.deepcopy(self.payload),
            session_id=self.session_id,
            turn_id=self.turn_id,
            timestamp=self.timestamp,
            sequence=self.sequence,
            schema_version=self.schema_version,
        )
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentEvent:
        """严格读取核心字段，同时允许新可选字段随旧 Reader 往返。"""

        required = ("event_id", "run_id", "runtime_id", "kind", "timestamp")
        for name in required:
            if not isinstance(payload.get(name), str) or not payload[name].strip():
                raise ValueError(f"AgentEvent {name} 必须是非空字符串")
        for name in ("session_id", "turn_id"):
            value = payload.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"AgentEvent {name} 必须是非空字符串或 null")
        sequence = payload.get("sequence", 0)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("AgentEvent sequence 必须是非负整数")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise ValueError("不支持 AgentEvent schema_version")
        data = payload.get("payload")
        if not isinstance(data, dict):
            raise TypeError("AgentEvent payload 必须是 JSON 对象")
        try:
            timestamp = datetime.fromisoformat(payload["timestamp"])
        except ValueError as exc:
            raise ValueError("AgentEvent timestamp 必须是 ISO 时间") from exc
        if timestamp.tzinfo is None:
            raise ValueError("AgentEvent timestamp 必须带时区")
        known = {
            *required,
            "payload", "session_id", "turn_id", "sequence", "schema_version",
        }
        return cls(
            event_id=payload["event_id"],
            run_id=payload["run_id"],
            runtime_id=payload["runtime_id"],
            kind=payload["kind"],
            payload=copy.deepcopy(data),
            session_id=payload.get("session_id"),
            turn_id=payload.get("turn_id"),
            timestamp=payload["timestamp"],
            sequence=sequence,
            schema_version=payload["schema_version"],
            extra=copy.deepcopy({key: value for key, value in payload.items() if key not in known}),
        )


EventSink = Callable[[AgentEvent], None]
