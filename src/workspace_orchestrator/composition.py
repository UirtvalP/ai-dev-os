"""具体 Runtime 只在进程入口组装，领域 Dispatcher 只接收执行端口。"""

from __future__ import annotations

from collections.abc import Callable

from .agent_runtime.contracts import AgentEvent, EventSink, RuntimeDescriptor
from .agent_runtime.events import RuntimeEventStore
from .agent_runtime.execution import RuntimeExecutor
from .agent_runtime.ports import AgentExecutionPort, AgentRuntimePort
from .agent_runtime.service import AgentRuntime
from .agent_runtime.stdio import JsonRpcStdioClient
from .executions import ExecutionStore
from .project_config import default_project_config, load_project_config
from .workbench import WorkbenchExecutionService
from .workspace import WorkspaceError, WorkspaceStore


def create_runtime(
    name: str, *, event_sink: EventSink | None = None,
    client_factory: Callable[..., JsonRpcStdioClient] = JsonRpcStdioClient,
) -> AgentRuntimePort:
    """显式选择已实现 Adapter；智能选择与插件注册属于后续 Policy 阶段。"""

    if name == "codex":
        from .agent_runtime.codex import CodexRuntime

        return CodexRuntime(event_sink=event_sink, client_factory=client_factory)
    if name == "cursor":
        from .agent_runtime.cursor import CursorAcpRuntime

        return CursorAcpRuntime(event_sink=event_sink, client_factory=client_factory)
    if name == "claude":
        from .agent_runtime.claude import ClaudeCliRuntime

        return ClaudeCliRuntime(event_sink=event_sink, client_factory=client_factory)
    raise WorkspaceError(f"未配置的 Agent Runtime：{name}")


def configured_executor(store: WorkspaceStore) -> AgentExecutionPort:
    config = (
        load_project_config(store.working_root)
        or load_project_config(store.project_root)
        or default_project_config(store.project_root)
    )

    def factory(sink: EventSink) -> AgentRuntimePort:
        return create_runtime(config.agent_runtime, event_sink=sink)

    return RuntimeExecutor(
        factory, RuntimeEventStore(store.root / "runtime-events"),
        allow_managed_hook_trust=config.agent_runtime == "codex",
        execution_store=ExecutionStore(store), runtime_id=config.agent_runtime,
    )


def runtime_descriptors() -> tuple[RuntimeDescriptor, ...]:
    result: list[RuntimeDescriptor] = []
    for name in ("codex", "cursor", "claude"):
        runtime = create_runtime(name)
        try:
            result.append(runtime.describe())
        finally:
            runtime.close()
    return tuple(result)


def create_standard_runtime(
    name: str, *, events: RuntimeEventStore,
    event_sink: EventSink | None = None,
    client_factory: Callable[..., JsonRpcStdioClient] = JsonRpcStdioClient,
) -> AgentRuntime:
    """为 Workbench 组装统一契约；具体 Adapter 仅在 Composition Root 出现。"""

    def persist(event: AgentEvent) -> None:
        stored = events.append(event)
        if event_sink is not None:
            event_sink(stored)

    return AgentRuntime(
        create_runtime(name, event_sink=persist, client_factory=client_factory), events,
    )


def configured_workbench(store: WorkspaceStore) -> WorkbenchExecutionService:
    """组装真实 Workbench 主路径；CLI Demo 可独立注入非权威 Runtime。"""

    return WorkbenchExecutionService(
        store, lambda name, events: create_standard_runtime(name, events=events),
    )
