"""Task 查询、唯一选择、创建与状态同步。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from workspace_orchestrator.adapters.base import TaskProvider
from workspace_orchestrator.adapters.task import (
    DashiTaskProvider,
    TaskProviderError,
    ensure_taskboard_service,
)
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore

from .requirement_attach import AutomationAmbiguity


@dataclass(frozen=True, slots=True)
class TaskSelection:
    task_ids: tuple[str, ...]
    tasks: tuple[Task, ...]


def configured_task_provider(meta: dict[str, object]) -> TaskProvider | None:
    if meta.get("task_provider") == "dashi":
        return DashiTaskProvider(
            project_id=str(meta.get("task_project_id") or "local"),
            service_starter=ensure_taskboard_service,
        )
    return None


def ensure_project_task_services(store: WorkspaceStore) -> None:
    """按项目已有 Requirement 配置启动外部任务服务；多次调用保持幂等。"""

    if not store.root.is_dir():
        return
    for meta_path in sorted(store.root.glob("REQ-*/meta.json")):
        meta = store.read_json(meta_path)
        if meta.get("task_provider") == "dashi":
            try:
                ensure_taskboard_service()
            except TaskProviderError as exc:
                raise WorkspaceError(f"任务面板自动启动失败：{exc}") from exc
            return


def _request_task(development_request: str) -> Task:
    request_text = " ".join(development_request.split())
    title = request_text if len(request_text) <= 120 else request_text[:117] + "..."
    return Task(
        id="new",
        title=title,
        description=development_request.strip(),
        status="in_progress",
    )


def select_tasks(
    requirement_id: str,
    task_provider: TaskProvider | None,
    *,
    explicit_task_ids: Sequence[str] = (),
    bound_task_ids: Sequence[str] = (),
    development_request: str | None = None,
) -> TaskSelection:
    """严格实现 Task Bootstrap 的确定性优先级。"""

    explicit = tuple(dict.fromkeys(explicit_task_ids))
    bound = tuple(dict.fromkeys(bound_task_ids))
    if task_provider is None:
        return TaskSelection(explicit or bound, ())
    try:
        tasks = tuple(task_provider.list_tasks(requirement_id))
        by_id = {task.id: task for task in tasks}
        # 已有 Session↔Task 绑定优先复用；显式 Task 只用于尚未绑定的 Session。
        selected = bound or explicit
        if selected:
            unknown = [task_id for task_id in selected if task_id not in by_id]
            if unknown:
                raise WorkspaceError(
                    f"Task 不属于需求 {requirement_id}：" + ", ".join(unknown)
                )
        else:
            active = [task for task in tasks if task.status == "in_progress"]
            if len(active) > 1:
                raise AutomationAmbiguity(
                    f"ambiguity：需求 {requirement_id} 存在多个 in_progress Task，"
                    "请使用 --task 明确指定：" + ", ".join(task.id for task in active)
                )
            if active:
                selected = (active[0].id,)
            elif development_request and development_request.strip():
                created = task_provider.create_task(
                    requirement_id, _request_task(development_request)
                )
                tasks = (*tasks, created)
                by_id[created.id] = created
                selected = (created.id,)
            else:
                raise WorkspaceError(
                    f"需求 {requirement_id} 没有可恢复的 in_progress Task；"
                    "请使用 --task 指定 Task，或使用 --request 提供当前开发请求以创建 Task"
                )
        # 可绑定但尚未开始的 Task 统一推进为 in_progress；不擅自重开审查或已完成 Task。
        refreshed = list(tasks)
        for task_id in selected:
            task = by_id[task_id]
            if task.status in {"todo", "ready"}:
                updated = task_provider.update_status(task_id, "in_progress")
                by_id[task_id] = updated
                refreshed = [updated if item.id == task_id else item for item in refreshed]
        return TaskSelection(tuple(selected), tuple(refreshed))
    except TaskProviderError as exc:
        raise WorkspaceError(f"Task Bootstrap 失败：{exc}") from exc


def move_tasks_to_review(task_provider: TaskProvider | None, task_ids: Sequence[str]) -> None:
    """验证成功后幂等推进到 in_review，永不自动 done。"""

    if task_provider is None:
        return
    for task_id in dict.fromkeys(task_ids):
        try:
            current = task_provider.get_task(task_id)
            if current.status not in {"in_review", "done"}:
                task_provider.update_status(task_id, "in_review")
        except TaskProviderError as exc:
            raise WorkspaceError(f"Task 状态同步失败：{exc}") from exc


def complete_tasks(task_provider: TaskProvider | None, task_ids: Sequence[str]) -> None:
    """在已推送自动收尾门禁通过后幂等完成关联开发 Task。"""

    if task_provider is None:
        raise WorkspaceError("当前 Requirement 未配置 Task Provider，无法自动完成任务")
    for task_id in dict.fromkeys(task_ids):
        try:
            current = task_provider.get_task(task_id)
            if current.status != "done":
                task_provider.update_status(task_id, "done")
        except TaskProviderError as exc:
            raise WorkspaceError(f"Task 自动完成失败：{exc}") from exc
