"""进入审查前的验收与验证门禁。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from .adapters.base import TaskProvider, TaskProviderError
from .intent import INTENT_CHECK_LABELS, IntentStatus, review_intent
from .models import Task
from .workspace import WorkspaceError, WorkspaceStore, bullets, markdown_sections, replace_section


def require_current_review_packet(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None,
    current_packet_fingerprint: str | None,
) -> Task | None:
    """对配置 Provider 的 Requirement 强制核对已发布 Packet 与当前事实。"""

    data = store.load(requirement_id)
    meta = data["meta"]
    if meta.get("task_provider") is None:
        return None
    if task_provider is None:
        raise WorkspaceError("已配置 Task Provider，无法验证 Review Packet 新鲜度")
    from .automation.task_attach import requirement_review_task
    from .review_packet import parse_review_packet_marker

    expected_id = meta.get("requirement_review_task_id")
    if not expected_id:
        raise WorkspaceError("Review Packet 尚未发布：缺少专用 Review 卡 ID")
    review_task = requirement_review_task(
        task_provider, requirement_id, expected_task_id=str(expected_id)
    )
    if review_task is None:
        raise WorkspaceError("Review Packet 尚未发布：缺少专用 Review 卡")
    marker = parse_review_packet_marker(review_task.description)
    revision = meta.get("review_packet_published_revision")
    fingerprint = meta.get("review_packet_published_fingerprint")
    if not revision or not fingerprint:
        raise WorkspaceError("Review Packet 尚未发布：缺少 revision/fingerprint")
    if (
        marker != (requirement_id.upper(), revision, fingerprint)
        or meta.get("review_packet_revision") != revision
        or meta.get("review_packet_fingerprint") != fingerprint
        or current_packet_fingerprint != fingerprint
    ):
        raise WorkspaceError(
            "Review Packet 已陈旧：marker、revision 或 fingerprint 与当前事实不一致"
        )
    return review_task


def request_requirement_changes(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    feedback: str,
    next_action: str | None = None,
    task_provider: TaskProvider | None = None,
    update_review_task: bool = True,
) -> None:
    """记录用户明确退回，并将可重开的开发 Task 恢复为 in_progress。"""

    feedback = feedback.strip()
    if not feedback:
        raise WorkspaceError("要求修改时必须提供明确反馈")
    feedback_key = hashlib.sha256(feedback.encode("utf-8")).hexdigest()
    with store.locked(requirement_id):
        data = store.load(requirement_id)
        if data["meta"].get("status") != "in_review":
            if (
                data["meta"].get("status") == "in_progress"
                and data["meta"].get("last_review_feedback_fingerprint") == feedback_key
            ):
                return
            raise WorkspaceError(
                "Requirement 必须处于 in_review 才能要求修改；"
                f"当前状态：{data['meta'].get('status')}"
            )
        state = replace_section(data["state"], "Phase", "implementation")
        state = replace_section(state, "In Progress", feedback)
        state = replace_section(state, "Next Action", next_action or feedback)
        previous = bullets(markdown_sections(state).get("Review Feedback", ""))
        record = f"{datetime.now().astimezone().isoformat(timespec='seconds')}：{feedback}"
        state = replace_section(
            state,
            "Review Feedback",
            "\n".join(f"- {item}" for item in (*previous, record)),
        )
        store.write_text(data["path"] / "state.md", state)
        store.touch_meta(
            requirement_id,
            status="in_progress",
            pending_task_review_reopen=task_provider is not None,
            last_review_feedback_fingerprint=feedback_key,
        )
    if task_provider is None:
        return
    pending = False
    try:
        from .automation.task_attach import is_requirement_review_task, is_requirement_space_task

        expected_id = data["meta"].get("requirement_review_task_id")
        for task in task_provider.list_tasks(requirement_id):
            if is_requirement_space_task(task):
                continue
            is_review = task.id == expected_id if expected_id else is_requirement_review_task(task)
            if is_review:
                if update_review_task and task.status != "in_progress":
                    task_provider.update_status(task.id, "in_progress")
            elif task.status == "in_review":
                task_provider.update_status(task.id, "in_progress")
    except TaskProviderError:
        pending = True
    store.touch_meta(requirement_id, pending_task_review_reopen=pending)


def sync_requirement_review_outcome(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None,
    *,
    current_packet_fingerprint: str | None = None,
) -> str | None:
    """同步专用 Review 卡的显式结果；Provider 不可用时保留本地状态。"""

    if task_provider is None:
        return None
    try:
        from .automation.task_attach import (
            ReviewTaskSyncError,
            is_requirement_review_task,
            is_requirement_space_task,
            requirement_review_task,
        )

        with store.provider_locked(requirement_id):
            data = store.load(requirement_id)
            status = data["meta"].get("status")
            if status == "in_progress" and data["meta"].get("pending_task_review_reopen"):
                expected_id = data["meta"].get("requirement_review_task_id")
                for task in task_provider.list_tasks(requirement_id):
                    if not is_requirement_space_task(task) and (
                        task.status == "in_review"
                        or (
                            (
                                task.id == expected_id
                                if expected_id
                                else is_requirement_review_task(task)
                            )
                            and task.status != "in_progress"
                        )
                    ):
                        task_provider.update_status(task.id, "in_progress")
                store.touch_meta(requirement_id, pending_task_review_reopen=False)
                return "已补偿同步用户要求修改的 Task 状态"
            if status != "in_review":
                return None
            expected_id = data["meta"].get("requirement_review_task_id")
            review_task = requirement_review_task(
                task_provider,
                requirement_id,
                expected_task_id=str(expected_id) if expected_id else None,
            )
            if review_task is None:
                store.touch_meta(requirement_id, status="in_progress")
                return "Review Packet 尚未发布：缺少专用 Review 卡；Requirement 已恢复 in_progress"
            from .review_packet import parse_review_packet_marker

            marker = parse_review_packet_marker(review_task.description)
            published_revision = data["meta"].get("review_packet_published_revision")
            published_fingerprint = data["meta"].get("review_packet_published_fingerprint")
            valid_packet = bool(
                marker
                and marker[0] == requirement_id.upper()
                and marker[1] == published_revision
                and marker[2] == published_fingerprint
                and current_packet_fingerprint == published_fingerprint
            )
            if not valid_packet:
                store.touch_meta(requirement_id, status="in_progress", review_packet_stale=True)
                return (
                    f"Requirement Review Task {review_task.id} 的 Review Packet 已陈旧或缺失；"
                    "旧 revision 不会批准当前 Requirement，已恢复 in_progress"
                )
            if review_task.status == "done":
                approval = task_provider.review_approval_fact(review_task.id)
                if approval is None:
                    store.touch_meta(
                        requirement_id,
                        review_approval_pending_reason="无法取得最后一次进入 done 的可靠 actor",
                    )
                    return (
                        f"Requirement Review Task {review_task.id} 已是 done，"
                        "但无法验证操作者；Requirement 保持 in_review"
                    )
                if approval.actor_type != "user" or not approval.actor_id:
                    reason = (
                        f"最后一次进入 done 的 actor 不是可靠用户："
                        f"{approval.actor_type or 'unknown'}:{approval.actor_id or 'unknown'}"
                    )
                    store.touch_meta(requirement_id, review_approval_pending_reason=reason)
                    return f"Requirement Review Task {review_task.id} 未获用户批准：{reason}"
                confirm_requirement_done(
                    store,
                    requirement_id,
                    user_confirmed=True,
                    task_provider=task_provider,
                    current_packet_fingerprint=current_packet_fingerprint,
                )
                store.touch_meta(
                    requirement_id,
                    review_confirmation_source=review_task.id,
                    review_confirmation_revision=published_revision,
                    review_confirmation_activity_id=approval.activity_id,
                    review_confirmation_actor_id=approval.actor_id,
                    review_approval_pending_reason=None,
                )
                return f"用户已在 dashi 批准 {review_task.id}，Requirement 已进入 done"
            if review_task.status == "in_progress":
                comments: tuple[str, ...] = ()
                list_comments = getattr(task_provider, "list_comments", None)
                if callable(list_comments):
                    comments = tuple(list_comments(review_task.id))
                baseline = int(data["meta"].get("review_comment_count") or 0)
                if len(comments) <= baseline:
                    return (
                        f"Requirement Review Task {review_task.id} 已退回 in_progress，"
                        "但没有本轮新留言；本地 Requirement 保持 in_review"
                    )
                feedback = comments[-1]
                request_requirement_changes(
                    store,
                    requirement_id,
                    feedback=feedback,
                    next_action=feedback,
                    task_provider=task_provider,
                    update_review_task=False,
                )
                store.touch_meta(requirement_id, review_comment_count=len(comments))
                return f"用户已在 dashi 退回 {review_task.id}，Requirement 已恢复 in_progress"
            return None
    except (TaskProviderError, ReviewTaskSyncError):
        return None


@dataclass(frozen=True, slots=True)
class ReviewResult:
    passed: bool
    blockers: tuple[str, ...]
    intent_status: IntentStatus


def review_requirement(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None = None,
    *,
    transition: bool = True,
) -> ReviewResult:
    with store.locked(requirement_id):
        return _review_requirement_locked(
            store, requirement_id, task_provider, transition=transition
        )


def _review_requirement_locked(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None = None,
    *,
    transition: bool = True,
) -> ReviewResult:
    data = store.load(requirement_id)
    blockers: list[str] = []
    intent_review = review_intent(data["intent"])
    if data["meta"].get("status") == "done":
        return ReviewResult(False, ("Requirement 已完成，不能重新进入审查",), intent_review.status)
    if intent_review.status is not IntentStatus.PASS:
        checks = "、".join(INTENT_CHECK_LABELS.get(name, name) for name in intent_review.incomplete)
        blockers.append(f"意图一致性状态为 {intent_review.status}：{checks}")
    requirement = markdown_sections(data["requirement"])
    criteria = re.findall(r"(?m)^- \[([ xX])\] (.+)$", requirement.get("Acceptance Criteria", ""))
    if not criteria:
        blockers.append("缺少验收标准")
    blockers.extend(f"验收标准尚未完成：{text}" for mark, text in criteria if mark == " ")

    verification = markdown_sections(data["verification"])
    statuses = re.findall(r"(?im)^(?:Status|状态)[:：]\s*(\S+)", "\n".join(verification.values()))
    if not statuses:
        blockers.append("缺少验证状态")
    # Historical evidence may be retained after a newer implementation replaces
    # it.  A SUPERSEDED section is explicitly archival rather than a failed
    # verification and must not prevent the current PASS evidence from closing
    # an otherwise complete requirement.
    blockers.extend(
        f"验证未通过：{status}"
        for status in statuses
        if not status.upper().startswith(("PASS", "SUPERSEDED"))
    )

    if task_provider is not None:
        try:
            from .automation.task_attach import (
                is_requirement_review_task,
                is_requirement_space_task,
            )

            tasks = task_provider.list_tasks(requirement_id)
            blockers.extend(
                f"任务尚未达到可审查状态：{task.id} [{task.status}]"
                for task in tasks
                if not is_requirement_review_task(task)
                and not is_requirement_space_task(task)
                and task.status not in {"in_review", "done"}
            )
        except TaskProviderError as exc:
            blockers.append(f"任务 Provider 不可用：{exc}")

    if not blockers and transition:
        store.touch_meta(requirement_id, status="in_review")
    elif data["meta"].get("status") == "in_review":
        # 审查依据已变化时，旧的 in_review 不能继续冒充有效状态。
        store.touch_meta(requirement_id, status="in_progress")
    return ReviewResult(not blockers, tuple(blockers), intent_review.status)


def confirm_requirement_done(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    user_confirmed: bool,
    task_provider: TaskProvider | None = None,
    current_packet_fingerprint: str | None = None,
) -> None:
    """仅在用户明确确认后执行 in_review → done；永不修改外部 Task。"""

    if not user_confirmed:
        raise WorkspaceError("必须提供用户明确确认，Requirement 才能进入 done")
    before = store.load(requirement_id)["meta"]
    require_current_review_packet(store, requirement_id, task_provider, current_packet_fingerprint)
    with store.locked(requirement_id):
        meta = store.load(requirement_id)["meta"]
        if meta.get("status") == "done":
            return
        if meta.get("status") != "in_review":
            raise WorkspaceError(
                f"Requirement 必须处于 in_review 才能确认完成；当前状态：{meta.get('status')}"
            )
        if before.get("task_provider") is not None and (
            meta.get("review_packet_published_revision")
            != before.get("review_packet_published_revision")
            or meta.get("review_packet_published_fingerprint")
            != before.get("review_packet_published_fingerprint")
        ):
            raise WorkspaceError("确认期间 Review Packet 已发生变化，请重新审查")
        store.touch_meta(requirement_id, status="done")
