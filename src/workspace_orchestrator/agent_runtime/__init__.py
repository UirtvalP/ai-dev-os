"""编辑器无关的 Agent 执行契约与可替换 Runtime。"""

from .contracts import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    ModelDescriptor,
    RuntimeDescriptor,
    RuntimeFailure,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from .events import RuntimeEventStore, RuntimeEventStoreError
from .ports import AgentExecutionPort, AgentRuntimePort

__all__ = [
    "AgentEvent",
    "AgentExecutionPort",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRuntimePort",
    "ModelDescriptor",
    "RuntimeDescriptor",
    "RuntimeEventStore",
    "RuntimeEventStoreError",
    "RuntimeFailure",
    "RuntimeOperationResult",
    "RuntimeSessionRef",
]
