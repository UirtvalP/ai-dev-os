"""Main Interactive Thread 到现有单 Worker Dispatcher 的非阻塞委派入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workspace_orchestrator.adapters.base import TaskProviderError
from workspace_orchestrator.models import Task, WorkflowComplexity
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore, now_iso

from .dispatcher import (
    _active_workspace_session,
    _read_state,
    _task_key,
    _task_state_allows_dispatch,
    _write_state,
    dispatcher_status,
    start_dispatcher,
)
from .task_attach import (
    configured_task_provider,
    is_requirement_review_task,
    is_requirement_space_task,
)


@dataclass(frozen=True, slots=True)
class DelegationDecision:
    delegate: bool
    reason: str


def decide_delegation(
    complexity: WorkflowComplexity, *, tiny_operation: bool = False
) -> DelegationDecision:
    """确定性表达 V1 默认策略；语义分类仍由 Main 完成。"""

    if tiny_operation or complexity == WorkflowComplexity.TINY:
        return DelegationDecision(False, "tiny 工作由 Main 直接执行")
    return DelegationDecision(True, f"{complexity.value} 工作的实际执行委派给 Dispatcher Worker")


def delegate_task(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    title: str,
    description: str,
    priority: str | None = None,
) -> dict[str, Any]:
    """持久化可执行 Task 并立即返回；绝不调用 CodexExecProvider。"""

    requirement_id = requirement_id.upper()
    data = store.load(requirement_id)
    provider = configured_task_provider(data["meta"], store.project_root)
    if provider is None:
        raise WorkspaceError("非阻塞委派需要已配置的 Task Provider")
    try:
        start = start_dispatcher(store, explicit=True)
    except OSError as exc:
        raise WorkspaceError(f"Dispatcher 启动失败，未创建委派 Task：{exc}") from exc
    if not start.get("running") or start.get("status") not in {"starting", "running"}:
        status = str(start.get("status") or "unknown")
        raise WorkspaceError(f"Dispatcher 未运行（{status}），未创建委派 Task")
    try:
        task = provider.create_task(
            requirement_id,
            Task(
                id="new",
                title=title.strip(),
                description=description.strip(),
                status="in_progress",
                priority=priority,
            ),
        )
    except TaskProviderError as exc:
        raise WorkspaceError(f"委派 Task 创建失败：{exc}") from exc
    return {
        "status": "queued",
        "requirement_id": requirement_id,
        "task_id": task.id,
        "title": task.title,
        "dispatcher": start.get("status"),
        "message": f"已委派 {task.id}：{task.title}。Worker 将独立执行，Main 可继续处理消息。",
    }


def worker_status(store: WorkspaceStore) -> dict[str, Any]:
    """合并 runtime state 与 Task Provider 业务状态，供 Main 快速查询。"""

    result = dispatcher_status(store)
    active = None
    queued: list[dict[str, str]] = []
    task_states = dict(result.get("tasks") or {})
    for requirement_id in store.requirement_ids(
        statuses={"draft", "ready", "in_progress", "blocked"}
    ):
        data = store.load(requirement_id)
        provider = configured_task_provider(data["meta"], store.project_root)
        if provider is None:
            continue
        try:
            tasks = provider.list_tasks(requirement_id)
        except TaskProviderError:  # Provider 不可用时仍返回本地 runtime state。
            continue
        for task in tasks:
            runtime = task_states.get(_task_key(task), {})
            if (
                task.status != "in_progress"
                or is_requirement_review_task(task)
                or is_requirement_space_task(task)
            ):
                continue
            item = {"requirement_id": requirement_id, "task_id": task.id, "title": task.title}
            if runtime.get("result") in {"dispatching", "running"}:
                active = {**item, **runtime}
                if not active.get("session_id") and task.binding_session_id:
                    active["session_id"] = task.binding_session_id
            elif (
                not task.binding_session_id
                and not _active_workspace_session(store, requirement_id, task.id)
                and _task_state_allows_dispatch(task, runtime)
                and runtime.get("result") not in {"cancel_requested", "cancelled"}
            ):
                queued.append(item)
    result["active_worker"] = active
    result["queued_tasks"] = queued
    return result


def request_cancel(store: WorkspaceStore, task_id: str) -> dict[str, Any]:
    """取消尚未启动的 queued Task；V1 不宣称可中断运行中的 Codex。"""

    matches: list[Task] = []
    for requirement_id in store.requirement_ids(
        statuses={"draft", "ready", "in_progress", "blocked"}
    ):
        data = store.load(requirement_id)
        provider = configured_task_provider(data["meta"], store.project_root)
        if provider is None:
            continue
        try:
            tasks = provider.list_tasks(requirement_id)
        except TaskProviderError:
            continue
        matches.extend(task for task in tasks if task.id == task_id or task.raw_id == task_id)
    unique = {_task_key(task): task for task in matches}
    if not unique:
        raise WorkspaceError(f"未找到可取消的 Task：{task_id}")
    if len(unique) > 1:
        raise WorkspaceError(f"Task 标识不唯一，无法安全取消：{task_id}")
    key, task = next(iter(unique.items()))
    if task.status != "in_progress" or is_requirement_review_task(task) or is_requirement_space_task(task):
        raise WorkspaceError(f"Task {task.id} 当前不是可取消的 queued 开发 Task")

    with store.locked():
        state = _read_state(store)
        tasks = dict(state.get("tasks") or {})
        runtime = dict(tasks.get(key) or {})
        if runtime.get("result") in {"dispatching", "running"} or task.binding_session_id:
            raise WorkspaceError(
                f"Task {task.id} 的 Worker 已运行；V1 只能取消尚未启动的 queued Task"
            )
        tasks[key] = {
            **runtime,
            "task_id": task.id,
            "raw_id": task.raw_id,
            "version": task.version,
            "result": "cancel_requested",
            "cancel_requested_at": now_iso(),
        }
        state["tasks"] = tasks
        _write_state(store, state)
    return {"status": "cancel_requested", "task_id": task.id}
