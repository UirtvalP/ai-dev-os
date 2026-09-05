"""具体 Runtime 只在进程入口组装，领域 Dispatcher 只接收执行端口。"""

from __future__ import annotations

from .agent_runtime.contracts import EventSink, RuntimeDescriptor
from .agent_runtime.events import RuntimeEventStore
from .agent_runtime.execution import RuntimeExecutor
from .agent_runtime.ports import AgentExecutionPort, AgentRuntimePort
from .project_config import default_project_config, load_project_config
from .workspace import WorkspaceError, WorkspaceStore


def create_runtime(name: str, *, event_sink: EventSink | None = None) -> AgentRuntimePort:
    """显式选择已实现 Adapter；智能选择与插件注册属于后续 Policy 阶段。"""

    if name == "codex":
        from .agent_runtime.codex import CodexRuntime

        return CodexRuntime(event_sink=event_sink)
    if name == "cursor":
        from .agent_runtime.cursor import CursorAcpRuntime

        return CursorAcpRuntime(event_sink=event_sink)
    if name == "claude":
        from .agent_runtime.claude import ClaudeCliRuntime

        return ClaudeCliRuntime(event_sink=event_sink)
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
