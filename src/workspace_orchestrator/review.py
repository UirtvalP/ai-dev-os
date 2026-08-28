"""进入审查前的验收与验证门禁。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .adapters.base import TaskProvider
from .adapters.task import DashiTaskProvider, TaskProviderError
from .intent import INTENT_CHECK_LABELS, IntentStatus, review_intent
from .workspace import WorkspaceStore, markdown_sections


@dataclass(frozen=True, slots=True)
class ReviewResult:
    passed: bool
    blockers: tuple[str, ...]
    intent_status: IntentStatus


def review_requirement(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None = None,
) -> ReviewResult:
    data = store.load(requirement_id)
    blockers: list[str] = []
    intent_review = review_intent(data["intent"])
    if intent_review.status is not IntentStatus.PASS:
        checks = "、".join(
            INTENT_CHECK_LABELS.get(name, name) for name in intent_review.incomplete
        )
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
    blockers.extend(f"验证未通过：{status}" for status in statuses if status.upper() != "PASS")

    if task_provider is None and data["meta"].get("task_provider") == "dashi":
        task_provider = DashiTaskProvider(
            project_id=data["meta"].get("task_project_id") or "local"
        )
    if task_provider is not None:
        try:
            tasks = task_provider.list_tasks(requirement_id)
            blockers.extend(
                f"任务尚未达到可审查状态：{task.id} [{task.status}]"
                for task in tasks
                if task.status not in {"in_review", "done"}
            )
        except TaskProviderError as exc:
            blockers.append(f"任务 Provider 不可用：{exc}")

    if not blockers:
        store.touch_meta(requirement_id, status="in_review")
    return ReviewResult(not blockers, tuple(blockers), intent_review.status)
