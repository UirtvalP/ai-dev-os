"""Task 查询、唯一选择、创建与状态同步。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from workspace_orchestrator.adapters.base import TaskProvider, TaskProviderError
from workspace_orchestrator.adapters.task import (
    DashiTaskProvider,
    ensure_taskboard_service,
)
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore, markdown_sections

from .requirement_attach import AutomationAmbiguity

REQUIREMENT_REVIEW_LABEL = "requirement-review"
REQUIREMENT_WORK_LABEL = "requirement-work"
REQUIREMENT_SPACE_LABEL = "requirement-space"
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


def is_requirement_space_task(task: Task) -> bool:
    """需求空间只承载人类可读状态，绝不能作为开发任务执行。"""

    return REQUIREMENT_SPACE_LABEL in task.labels


def _requirement_space_content(store: WorkspaceStore, requirement_id: str) -> str:
    data = store.load(requirement_id)
    meta = data["meta"]
    requirement = markdown_sections(data["requirement"])
    state = markdown_sections(data["state"])
    verification = markdown_sections(data["verification"])
    verification_summary = (
        "\n\n".join(f"### {name}\n{body.strip()}" for name, body in verification.items()) or "无"
    )
    return (
        f"# {requirement_id} 需求空间\n\n"
        f"## 目标\n\n{requirement.get('Goal', meta['title']).strip()}\n\n"
        f"## 当前状态\n\n- 状态：{meta['status']}\n"
        f"- 阶段：{state.get('Phase', '未知').strip() or '未知'}\n"
        f"- 最近更新：{meta.get('updated_at', '未知')}\n\n"
        f"## 已完成\n\n{state.get('Completed', '无').strip() or '无'}\n\n"
        f"## 待处理\n\n{state.get('Pending', '无').strip() or '无'}\n\n"
        f"## 阻塞项\n\n{state.get('Blocked', '无').strip() or '无'}\n\n"
        f"## 验证\n\n{verification_summary}\n\n---\n\n"
        "将此卡移至 `done` 仅表示关闭该需求空间的面板可见性；"
        "不会删除 Requirement、开发任务或历史记录。"
    )


def _requirement_space_task(
    store: WorkspaceStore, requirement_id: str, *, title: str
) -> Task:
    return Task(
        id="new",
        title=title,
        description=_requirement_space_content(store, requirement_id),
        status="todo",
        labels=(REQUIREMENT_SPACE_LABEL, f"requirement:{requirement_id}"),
    )


def _validate_requirement_space_identity(
    task: Task,
    *,
    requirement_id: str,
    expected_title: str,
    expected_project_id: str | None,
    allow_legacy_projection: bool,
) -> None:
    expected_requirement_label = f"requirement:{requirement_id}"
    marker = f"# {requirement_id} 需求空间"
    first_line = task.description.splitlines()[0].strip() if task.description else ""
    requirement_labels = {label for label in task.labels if label.startswith("requirement:")}
    full_labels = {
        REQUIREMENT_SPACE_LABEL,
        expected_requirement_label,
    }.issubset(task.labels)
    legacy_labels = allow_legacy_projection and not task.labels
    if expected_project_id and task.project_id and task.project_id != expected_project_id:
        raise WorkspaceError(f"Requirement 空间卡 {task.id} 属于另一个 Task 项目")
    if task.title != expected_title or first_line != marker:
        raise WorkspaceError(f"Requirement 空间卡 {task.id} 的标题或正文身份标记不匹配")
    if task.binding_session_id is not None:
        raise WorkspaceError(f"Requirement 空间卡 {task.id} 不能带执行 Session 绑定")
    if requirement_labels not in (set(), {expected_requirement_label}):
        raise WorkspaceError(f"Requirement 空间卡 {task.id} 带有其他 Requirement 标签")
    if not full_labels and not legacy_labels:
        raise WorkspaceError(f"Requirement 空间卡 {task.id} 的身份标签不完整")


def ensure_requirement_space_task(
    store: WorkspaceStore, requirement_id: str, task_provider: TaskProvider | None
) -> Task | None:
    """幂等投影不绑定 Thread 的 Requirement 概览卡。"""

    if task_provider is None:
        return None
    try:
        with store.provider_locked(requirement_id):
            data = store.load(requirement_id)
            meta = data["meta"]
            expected_title = f"[需求空间] {requirement_id} {meta['title']}"
            expected_project_id = str(meta.get("task_project_id") or "") or None
            desired = _requirement_space_task(
                store, requirement_id, title=expected_title
            )
            tasks = tuple(task_provider.list_tasks(requirement_id))
            expected_id = str(meta.get("requirement_space_task_id") or "")
            current = next((task for task in tasks if task.id == expected_id), None)
            if expected_id and current is None:
                try:
                    candidate = task_provider.get_task(expected_id)
                except TaskProviderError as exc:
                    raise BoardTaskSyncError(
                        f"无法核验需求 {requirement_id} 已记录的 Requirement 空间卡 "
                        f"{expected_id}；为避免创建重复卡，已停止同步：{exc}"
                    ) from exc
                _validate_requirement_space_identity(
                    candidate,
                    requirement_id=requirement_id,
                    expected_title=expected_title,
                    expected_project_id=expected_project_id,
                    allow_legacy_projection=True,
                )
                current = candidate
            candidates = {
                task.id: task for task in tasks if is_requirement_space_task(task)
            }
            finder = getattr(task_provider, "find_tasks_by_exact_title", None)
            if callable(finder):
                candidates.update(
                    (task.id, task) for task in finder(expected_title)
                )
            if current is not None:
                candidates[current.id] = current
            for candidate in candidates.values():
                _validate_requirement_space_identity(
                    candidate,
                    requirement_id=requirement_id,
                    expected_title=expected_title,
                    expected_project_id=expected_project_id,
                    allow_legacy_projection=True,
                )
            if len(candidates) > 1:
                raise WorkspaceError(
                    f"需求 {requirement_id} 存在多个 Requirement 空间卡："
                    + ", ".join(sorted(candidates))
                )
            if current is None and candidates:
                current = next(iter(candidates.values()))
            if current is not None and current.status == "done":
                if (
                    meta.get("requirement_space_task_id") != current.id
                    or meta.get("requirement_space_closed") is not True
                ):
                    store.touch_meta(
                        requirement_id,
                        requirement_space_task_id=current.id,
                        requirement_space_closed=True,
                    )
                return current
            if current is None:
                if meta.get("requirement_space_closed"):
                    return None
                current = task_provider.create_task(requirement_id, desired)
                _validate_requirement_space_identity(
                    current,
                    requirement_id=requirement_id,
                    expected_title=expected_title,
                    expected_project_id=expected_project_id,
                    allow_legacy_projection=False,
                )
            else:
                reconcile = getattr(task_provider, "reconcile_task", None)
                if callable(reconcile):
                    current = reconcile(requirement_id, current.id, desired)
                else:
                    _validate_requirement_space_identity(
                        current,
                        requirement_id=requirement_id,
                        expected_title=expected_title,
                        expected_project_id=expected_project_id,
                        allow_legacy_projection=False,
                    )
                    publish = getattr(task_provider, "publish_review", None)
                    if callable(publish):
                        current = publish(current.id, desired.description)
                _validate_requirement_space_identity(
                    current,
                    requirement_id=requirement_id,
                    expected_title=expected_title,
                    expected_project_id=expected_project_id,
                    allow_legacy_projection=False,
                )
                if current.status != desired.status:
                    raise WorkspaceError(
                        f"Requirement 空间卡 {current.id} 状态未收敛到 {desired.status}"
                    )
            if (
                meta.get("requirement_space_task_id") != current.id
                or meta.get("requirement_space_closed") is not False
            ):
                store.touch_meta(
                    requirement_id,
                    requirement_space_task_id=current.id,
                    requirement_space_closed=False,
                )
            return current
    except TaskProviderError as exc:
        raise BoardTaskSyncError(f"Requirement 空间同步失败：{exc}") from exc


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
                if not is_requirement_review_task(task) and not is_requirement_space_task(task)
            )
            expected_id = str(meta.get("requirement_task_id") or "")
            phase_gate_required = meta.get("phase_gate_required")
            if phase_gate_required is not None and not isinstance(
                phase_gate_required, bool
            ):
                raise BoardTaskSyncError("phase_gate_required 必须是布尔值")
            if phase_gate_required:
                if not expected_id:
                    raise BoardTaskSyncError(
                        f"{requirement_id} 已启用阶段门禁，但没有权威当前 Task"
                    )
                current = next((task for task in tasks if task.id == expected_id), None)
                if current is None:
                    raise BoardTaskSyncError(
                        f"{requirement_id} 的权威阶段 Task {expected_id} 在 Provider 中不可见；"
                        "拒绝创建或改绑替代工作卡"
                    )
                # Local import keeps task helpers independent for ungated V1 users while
                # forcing every gated board projection through the committed phase chain.
                from workspace_orchestrator.phase_gate import GateStore

                GateStore(store).require_task_active(requirement_id, current.id)
                if meta.get("pending_task_visibility"):
                    store.touch_meta(requirement_id, pending_task_visibility=False)
                return current
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


def ensure_requirement_task_parent(
    store: WorkspaceStore, requirement_id: str, task_provider: TaskProvider | None
) -> None:
    """让 Requirement 空间成为开发 Task 的父节点；Thread 仍只绑定子 Task。"""

    if task_provider is None:
        return
    data = store.load(requirement_id)
    parent_id = str(data["meta"].get("requirement_space_task_id") or "")
    if not parent_id:
        return
    setter = getattr(task_provider, "set_parent", None)
    if not callable(setter):
        return
    try:
        for task in task_provider.list_tasks(requirement_id):
            if not is_requirement_space_task(task) and not is_requirement_review_task(task):
                setter(task.id, parent_id)
    except TaskProviderError as exc:
        raise BoardTaskSyncError(f"Requirement 任务层级同步失败：{exc}") from exc


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
    task_activation_guard: Callable[[Task], None] | None = None,
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
            if not is_requirement_review_task(task) and not is_requirement_space_task(task)
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
                    if REQUIREMENT_WORK_LABEL in task.labels and task.status in {"todo", "ready"}
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
            if task_activation_guard is not None:
                task_activation_guard(task)
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
