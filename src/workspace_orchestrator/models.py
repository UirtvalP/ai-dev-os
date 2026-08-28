"""与 Provider 无关的 V1 领域模型。"""

from dataclasses import dataclass, field
from enum import StrEnum


class WorkflowComplexity(StrEnum):
    TINY = "tiny"
    NORMAL = "normal"
    COMPLEX = "complex"
    RESEARCH = "research"


class RequirementStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    title: str
    goal: str
    acceptance_criteria: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    title: str
    raw_id: str | None = None
    project_id: str | None = None
    description: str = ""
    status: str = "todo"
    priority: str | None = None
    parent_id: str | None = None
    blocked_by: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    branch: str | None = None
    worktree: str | None = None
    session_ids: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    version: int | None = None


@dataclass(slots=True)
class Workspace:
    requirement: Requirement
    status: RequirementStatus = RequirementStatus.DRAFT
    complexity: WorkflowComplexity = WorkflowComplexity.NORMAL
    tasks: list[Task] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Session:
    """不保存对话历史的可替换 Agent 会话记录。"""

    id: str
    agent: str
    started_at: str
    ended_at: str | None = None
    task_ids: tuple[str, ...] = ()
    result: str = "in_progress"
