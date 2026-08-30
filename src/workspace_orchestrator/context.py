"""生命周期兼容 API；实现由确定性 Automation Layer 持有。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .adapters.base import AgentProvider, TaskProvider
from .automation.runtime import AutomationRuntime
from .workspace import WorkspaceStore


@dataclass(frozen=True, slots=True)
class _NoAgentProvider:
    @property
    def name(self) -> str:
        return "unknown"

    def current_session_id(self) -> None:
        return None


def _runtime(
    store: WorkspaceStore,
    agent_provider: AgentProvider | None,
    task_provider: TaskProvider | None,
) -> AutomationRuntime:
    return AutomationRuntime(store, agent_provider or _NoAgentProvider(), task_provider)


def build_snapshot(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None = None,
    agent_provider: AgentProvider | None = None,
    task_ids: Sequence[str] = (),
) -> str:
    return _runtime(store, agent_provider, task_provider).snapshot(
        requirement_id, task_ids=task_ids
    )


def bootstrap_session(
    store: WorkspaceStore,
    requirement_id: str | None = None,
    *,
    agent_provider: AgentProvider,
    task_provider: TaskProvider | None = None,
    task_ids: Sequence[str] = (),
    development_request: str | None = None,
    creation_key: str | None = None,
) -> str:
    return _runtime(store, agent_provider, task_provider).bootstrap(
        requirement_id,
        task_ids=task_ids,
        development_request=development_request,
        creation_key=creation_key,
    )


def checkpoint(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    phase: str | None = None,
    completed: list[str] | None = None,
    next_action: str | None = None,
    verification: str | None = None,
    agent_provider: AgentProvider | None = None,
    task_provider: TaskProvider | None = None,
    task_ids: Sequence[str] = (),
) -> None:
    _runtime(store, agent_provider, task_provider).checkpoint(
        requirement_id,
        phase=phase,
        completed=completed or (),
        next_action=next_action,
        verification=verification,
        task_ids=task_ids,
    )


def handoff(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    completed: list[str] | None = None,
    files_changed: list[str] | None = None,
    current_state: str | None = None,
    important_context: str | None = None,
    next_action: str | None = None,
    known_problems: str | None = None,
    agent_provider: AgentProvider | None = None,
    task_provider: TaskProvider | None = None,
    task_ids: Sequence[str] = (),
) -> None:
    _runtime(store, agent_provider, task_provider).handoff(
        requirement_id,
        completed=completed or (),
        files_changed=files_changed or (),
        current_state=current_state,
        important_context=important_context,
        next_action=next_action,
        known_problems=known_problems,
        task_ids=task_ids,
    )
