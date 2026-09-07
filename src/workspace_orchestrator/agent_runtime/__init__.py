"""编辑器无关的 Agent 执行契约与可替换 Runtime。"""

from .contracts import (
    CANONICAL_RUNTIME_CAPABILITIES,
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    ExecutionSpec,
    ModelDescriptor,
    RuntimeDescriptor,
    RuntimeFailure,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from .events import RuntimeEventStore, RuntimeEventStoreError
from .ports import AgentExecutionPort, AgentRuntimePort, StandardAgentRuntimePort
from .service import AgentRuntime

__all__ = [
    "CANONICAL_RUNTIME_CAPABILITIES",
    "AgentEvent",
    "AgentExecutionPort",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRuntime",
    "AgentRuntimePort",
    "ExecutionSpec",
    "ModelDescriptor",
    "RuntimeDescriptor",
    "RuntimeEventStore",
    "RuntimeEventStoreError",
    "RuntimeFailure",
    "RuntimeOperationResult",
    "RuntimeSessionRef",
    "StandardAgentRuntimePort",
]
