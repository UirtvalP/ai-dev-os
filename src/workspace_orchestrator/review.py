"""Acceptance and verification gate for entering review."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .adapters.base import TaskProvider
from .adapters.task import DashiTaskProvider, TaskProviderError
from .workspace import WorkspaceStore, markdown_sections


@dataclass(frozen=True, slots=True)
class ReviewResult:
    passed: bool
    blockers: tuple[str, ...]


def review_requirement(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None = None,
) -> ReviewResult:
    data = store.load(requirement_id)
    blockers: list[str] = []
    requirement = markdown_sections(data["requirement"])
    criteria = re.findall(r"(?m)^- \[([ xX])\] (.+)$", requirement.get("Acceptance Criteria", ""))
    if not criteria:
        blockers.append("Acceptance criteria are missing")
    blockers.extend(f"Acceptance criterion is incomplete: {text}" for mark, text in criteria if mark == " ")

    verification = markdown_sections(data["verification"])
    statuses = re.findall(r"(?im)^Status:\s*(\S+)", "\n".join(verification.values()))
    if not statuses:
        blockers.append("Verification statuses are missing")
    blockers.extend(f"Verification is not passing: {status}" for status in statuses if status.upper() != "PASS")

    if task_provider is None and data["meta"].get("task_provider") == "dashi":
        task_provider = DashiTaskProvider(
            project_id=data["meta"].get("task_project_id") or "local"
        )
    if task_provider is not None:
        try:
            tasks = task_provider.list_tasks(requirement_id)
            blockers.extend(
                f"Task is not review-ready: {task.id} [{task.status}]"
                for task in tasks
                if task.status not in {"in_review", "done"}
            )
        except TaskProviderError as exc:
            blockers.append(f"Task provider is unavailable: {exc}")

    if not blockers:
        store.touch_meta(requirement_id, status="in_review")
    return ReviewResult(not blockers, tuple(blockers))
