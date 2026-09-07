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
STANDARD_EVENT_KINDS = frozenset({
    "session", "turn", "message", "tool", "approval", "error", "completion", "unknown",
})

CANONICAL_RUNTIME_CAPABILITIES = (
    "start",
    "resume",
    "interactive_message",
    "event_stream",
    "cancel",
    "status",
    "archive",
    "model_selection",
    "reasoning_selection",
    "approval",
    "tool_events",
    "diff_events",
)

_CAPABILITY_ALIASES: dict[str, frozenset[str]] = {
    "interactive_message": frozenset(("interactive_message", "message")),
    "event_stream": frozenset(("event_stream", "events")),
    "cancel": frozenset(("cancel", "interrupt")),
    "status": frozenset(("status", "read")),
    "model_selection": frozenset(("model_selection", "models")),
    "approval": frozenset(("approval", "approval_response")),
    "tool_events": frozenset(("tool_events", "events")),
}
_CAPABILITY_CANONICAL_BY_ALIAS = {
    "message": "interactive_message",
    "events": "event_stream",
    "interrupt": "cancel",
    "read": "status",
    "models": "model_selection",
    "approval_response": "approval",
}


def standard_event_kind(detail: str) -> str:
    """Adapter 对外只发布统一类别；结束事件不代表成功或需求完成。"""

    if detail in {"turn.completed", "turn.failed", "turn.cancelled"}:
        return "completion"
    category = detail.partition(".")[0]
    return category if category in STANDARD_EVENT_KINDS else "unknown"


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
        if not self.available:
            return False
        canonical = _CAPABILITY_CANONICAL_BY_ALIAS.get(capability, capability)
        if canonical == "reasoning_selection":
            return any(model.reasoning_efforts for model in self.models)
        aliases = _CAPABILITY_ALIASES.get(canonical, frozenset((canonical,)))
        return not aliases.isdisjoint(self.capabilities)

    @property
    def canonical_capabilities(self) -> tuple[str, ...]:
        """返回稳定契约词汇，同时保留 Adapter 的旧能力名用于兼容。"""

        return tuple(
            capability for capability in CANONICAL_RUNTIME_CAPABILITIES
            if self.supports(capability)
        )


@dataclass(frozen=True, slots=True)
class RuntimeSessionRef:
    runtime_id: str
    session_id: str
    run_id: str = ""
    workspace_path: str = ""
    schema_version: int = SCHEMA_VERSION
    execution_id: str | None = None
    sandbox: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    requirement_id: str | None = None
    task_id: str | None = None


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
    reasoning_effort: str | None = None
    execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """Workbench 启动 Runtime 的 Provider 无关输入。"""

    run_id: str
    workspace_path: Path
    message: str
    execution_id: str
    sandbox: str = "workspace-write"
    model: str | None = None
    reasoning_effort: str | None = None
    requirement_id: str | None = None
    task_id: str | None = None
    bypass_hook_trust: bool = False
    timeout_seconds: float = 7200
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.execution_id.strip():
            raise ValueError("ExecutionSpec run_id 与 execution_id 必须是非空字符串")

    def to_request(self, *, resume_session_id: str | None = None) -> AgentRunRequest:
        return AgentRunRequest(
            run_id=self.run_id,
            workspace_path=self.workspace_path,
            prompt=self.message,
            sandbox=self.sandbox,
            model=self.model,
            resume_session_id=resume_session_id,
            bypass_hook_trust=self.bypass_hook_trust,
            timeout_seconds=self.timeout_seconds,
            requirement_id=self.requirement_id,
            task_id=self.task_id,
            reasoning_effort=self.reasoning_effort,
            execution_id=self.execution_id,
        )


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
    requirement_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None

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
            requirement_id=self.requirement_id,
            task_id=self.task_id,
            execution_id=self.execution_id,
        )
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentEvent:
        """严格读取核心字段，同时允许新可选字段随旧 Reader 往返。"""

        required = ("event_id", "run_id", "runtime_id", "kind", "timestamp")
        for name in required:
            if not isinstance(payload.get(name), str) or not payload[name].strip():
                raise ValueError(f"AgentEvent {name} 必须是非空字符串")
        for name in ("session_id", "turn_id", "requirement_id", "task_id", "execution_id"):
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
            "requirement_id", "task_id", "execution_id",
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
            requirement_id=payload.get("requirement_id"),
            task_id=payload.get("task_id"),
            execution_id=payload.get("execution_id"),
            extra=copy.deepcopy({key: value for key, value in payload.items() if key not in known}),
        )


EventSink = Callable[[AgentEvent], None]
