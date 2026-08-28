"""Evidence-based workflow selection without a workflow DSL."""

from __future__ import annotations

from dataclasses import dataclass

from .models import WorkflowComplexity
from .workspace import WorkspaceError, WorkspaceStore, now_iso


@dataclass(frozen=True, slots=True)
class WorkflowDecision:
    complexity: WorkflowComplexity
    reason: str


def route_workflow(text: str) -> WorkflowDecision:
    normalized = text.casefold()
    research_terms = ("research", "investigate", "root cause unknown", "调研", "技术选型", "性能调查")
    complex_terms = (
        "architecture",
        "migration",
        "schema",
        "breaking api",
        "new system",
        "架构",
        "迁移",
        "跨模块",
        "新系统",
    )
    tiny_terms = (
        "typo",
        "copy change",
        "null check",
        "small config",
        "文案",
        "错别字",
        "空指针",
        "小配置",
    )
    if any(term in normalized for term in research_terms):
        return WorkflowDecision(WorkflowComplexity.RESEARCH, "The request explicitly requires investigation.")
    if any(term in normalized for term in complex_terms):
        return WorkflowDecision(WorkflowComplexity.COMPLEX, "The request has cross-module or architectural risk.")
    if any(term in normalized for term in tiny_terms):
        return WorkflowDecision(WorkflowComplexity.TINY, "The expected change is explicit and local.")
    return WorkflowDecision(WorkflowComplexity.NORMAL, "Normal is the safe default without stronger evidence.")


def upgrade_workflow(
    store: WorkspaceStore,
    requirement_id: str,
    target: WorkflowComplexity,
    reason: str,
) -> None:
    if not reason.strip():
        raise WorkspaceError("Workflow upgrades require an evidence-based reason")
    data = store.load(requirement_id)
    current = WorkflowComplexity(data["meta"]["workflow"])
    rank = {
        WorkflowComplexity.TINY: 0,
        WorkflowComplexity.NORMAL: 1,
        WorkflowComplexity.COMPLEX: 2,
    }
    if current is WorkflowComplexity.RESEARCH or target is WorkflowComplexity.RESEARCH:
        raise WorkspaceError("Research workflow must be selected explicitly at requirement creation")
    if rank[target] <= rank[current]:
        raise WorkspaceError(f"Workflow can only upgrade: {current.value} -> {target.value}")
    history = list(data["meta"].get("workflow_history", []))
    history.append(
        {"from": current.value, "to": target.value, "reason": reason.strip(), "at": now_iso()}
    )
    store.touch_meta(
        requirement_id,
        complexity=target.value,
        workflow=target.value,
        workflow_history=history,
    )
