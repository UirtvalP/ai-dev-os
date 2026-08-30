"""Task 查询、唯一选择、创建与状态同步。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from workspace_orchestrator.adapters.base import TaskProvider
from workspace_orchestrator.adapters.task import (
    DashiTaskProvider,
    TaskProviderError,
    ensure_taskboard_service,
)
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore, markdown_sections

from .requirement_attach import AutomationAmbiguity

REQUIREMENT_REVIEW_LABEL = "requirement-review"
REQUIREMENT_WORK_LABEL = "requirement-work"
BOARD_VISIBLE_STATUSES = {"todo", "ready", "in_progress", "blocked", "in_review"}


class ReviewTaskSyncError(WorkspaceError):
    """专用 Review Task 的可重试 Provider 同步失败。"""


class BoardTaskSyncError(WorkspaceError):
    """Requirement 工作卡的可重试 Provider 同步失败。"""


@dataclass(frozen=True, slots=True)
class TaskSelection:
    task_ids: tuple[str, ...]
    tasks: tuple[Task, ...]
    task_error: str | None = None


def configured_task_provider(
    meta: dict[str, object], project_root: Path | None = None
) -> TaskProvider | None:
    if meta.get("task_provider") == "dashi":
        return DashiTaskProvider(
            project_id=str(meta.get("task_project_id") or "local"),
            service_starter=ensure_taskboard_service,
            project_name=(project_root.name if project_root else None),
            workspace_path=(str(project_root.resolve()) if project_root else None),
        )
    if meta.get("task_provider") is None:
        return None
    raise WorkspaceError(f"V1 不支持的 Task Provider：{meta.get('task_provider')}")


def _request_task(development_request: str) -> Task:
    request_text = " ".join(development_request.split())
    title = request_text if len(request_text) <= 120 else request_text[:117] + "..."
    return Task(
        id="new",
        title=title,
        description=development_request.strip(),
        status="in_progress",
    )


def is_requirement_review_task(task: Task) -> bool:
    return REQUIREMENT_REVIEW_LABEL in task.labels


def ensure_requirement_board_task(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None,
) -> Task | None:
    """为每个未完成 Requirement 保证一张可在主面板继续执行的活动工作卡。"""

    if task_provider is None:
        return None
    try:
        with store.provider_locked(requirement_id):
            data = store.load(requirement_id)
            meta = data["meta"]
            if meta.get("status") == "done":
                return None
            tasks = tuple(
                task
                for task in task_provider.list_tasks(requirement_id)
                if not is_requirement_review_task(task)
            )
            expected_id = str(meta.get("requirement_task_id") or "")
            visible = tuple(task for task in tasks if task.status in BOARD_VISIBLE_STATUSES)
            current = next((task for task in visible if task.id == expected_id), None)
            if current is None and visible:
                current = next(
                    (task for task in visible if REQUIREMENT_WORK_LABEL in task.labels),
                    visible[0],
                )
            if current is not None:
                if expected_id != current.id or meta.get("pending_task_visibility"):
                    store.touch_meta(
                        requirement_id,
                        requirement_task_id=current.id,
                        pending_task_visibility=False,
                    )
                return current
            sections = markdown_sections(data["requirement"])
            desired_status = (
                "blocked"
                if meta.get("status") == "blocked"
                else "in_review"
                if meta.get("status") == "in_review"
                else "todo"
            )
            created = task_provider.create_task(
                requirement_id,
                Task(
                    id="new",
                    title=f"{requirement_id} {meta['title']}",
                    description=sections.get("Goal", str(meta["title"])),
                    status=desired_status,
                    labels=(REQUIREMENT_WORK_LABEL,),
                ),
            )
            store.touch_meta(
                requirement_id,
                requirement_task_id=created.id,
                pending_task_visibility=False,
            )
            return created
    except TaskProviderError as exc:
        store.touch_meta(requirement_id, pending_task_visibility=True)
        raise BoardTaskSyncError(f"Requirement 工作卡同步失败：{exc}") from exc


def requirement_review_task(
    task_provider: TaskProvider,
    requirement_id: str,
    expected_task_id: str | None = None,
) -> Task | None:
    tasks = tuple(task_provider.list_tasks(requirement_id))
    if expected_task_id:
        current = next((task for task in tasks if task.id == expected_task_id), None)
        if current is None:
            raise WorkspaceError(f"Requirement Review Task 不存在：{expected_task_id}")
        if not is_requirement_review_task(current):
            raise WorkspaceError(
                f"Task {expected_task_id} 缺少 {REQUIREMENT_REVIEW_LABEL} 身份标签"
            )
        return current
    matches = tuple(task for task in tasks if is_requirement_review_task(task))
    if len(matches) > 1:
        raise WorkspaceError(
            f"需求 {requirement_id} 存在多个 Requirement Review Task："
            + ", ".join(task.id for task in matches)
        )
    return matches[0] if matches else None


def ensure_requirement_review_task(
    task_provider: TaskProvider | None,
    requirement_id: str,
    title: str,
    expected_task_id: str | None = None,
) -> Task | None:
    """创建或重置专用审查卡；Runtime 永不把它推进到 done。"""

    if task_provider is None:
        return None
    try:
        current = requirement_review_task(
            task_provider, requirement_id, expected_task_id=expected_task_id
        )
        if current is None:
            return task_provider.create_task(
                requirement_id,
                Task(
                    id="new",
                    title=f"[Requirement Review] {requirement_id} {title}",
                    description="Review Packet 正在生成；材料发布完成前不得批准。",
                    status="in_progress",
                    labels=(REQUIREMENT_REVIEW_LABEL,),
                ),
            )
        if current.status == "done":
            # 仅会在旧 Packet 已被 Runtime 判定陈旧并恢复本地 in_progress 后执行；
            # 重新打开用于发布新 revision，绝不把有效用户批准覆盖掉。
            return task_provider.update_status(current.id, "in_progress")
        return current
    except TaskProviderError as exc:
        raise ReviewTaskSyncError(f"Requirement Review Task 同步失败：{exc}") from exc


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
        tasks = tuple(
            task
            for task in task_provider.list_tasks(requirement_id)
            if not is_requirement_review_task(task)
        )
        by_id = {task.id: task for task in tasks}
        # 已有 Session↔Task 绑定优先复用；显式 Task 只用于尚未绑定的 Session。
        selected = bound or explicit
        if selected:
            unknown = [task_id for task_id in selected if task_id not in by_id]
            if unknown:
                raise WorkspaceError(f"Task 不属于需求 {requirement_id}：" + ", ".join(unknown))
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
                work_cards = [
                    task
                    for task in tasks
                    if REQUIREMENT_WORK_LABEL in task.labels
                    and task.status in {"todo", "ready"}
                ]
                if len(work_cards) == 1:
                    selected = (work_cards[0].id,)
                else:
                    created = task_provider.create_task(
                        requirement_id, _request_task(development_request)
                    )
                    tasks = (*tasks, created)
                    by_id[created.id] = created
                    selected = (created.id,)
            else:
                # SessionStart 可能没有开发请求；先恢复 Requirement，首次用户请求
                # 到达时再创建开发 Task，不能让空任务板阻断本地开发。
                selected = ()
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
        # 外部 Provider 离线时仍恢复本地 Requirement；后续 bootstrap 会重新同步。
        return TaskSelection(explicit or bound, (), str(exc))


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
    """已推送自动收尾门禁通过后，幂等完成关联开发 Task。"""

    if task_provider is None:
        raise WorkspaceError("当前 Requirement 未配置 Task Provider，无法自动完成任务")
    for task_id in dict.fromkeys(task_ids):
        try:
            current = task_provider.get_task(task_id)
            if current.status != "done":
                task_provider.update_status(task_id, "done")
        except TaskProviderError as exc:
            raise WorkspaceError(f"Task 自动完成失败：{exc}") from exc
