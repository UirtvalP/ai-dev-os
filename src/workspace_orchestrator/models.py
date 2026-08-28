"""Provider-neutral V1 domain models."""

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
    status: str = "todo"
    blocked_by: tuple[str, ...] = ()


@dataclass(slots=True)
class Workspace:
    requirement: Requirement
    status: RequirementStatus = RequirementStatus.DRAFT
    complexity: WorkflowComplexity = WorkflowComplexity.NORMAL
    tasks: list[Task] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
