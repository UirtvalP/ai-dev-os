"""不依赖工作流 DSL、基于证据的工作流选择。"""

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
    research_terms = (
        "research",
        "investigate",
        "root cause unknown",
        "调研",
        "技术选型",
        "性能调查",
    )
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
        "bug fix",
        "fix bug",
        "fix a bug",
        "fix ",
        "small fix",
        "small change",
        "minor change",
        "quick fix",
        "tweak",
        "null check",
        "small config",
        "文案",
        "错别字",
        "修 bug",
        "修复 bug",
        "修复bug",
        "修复",
        "小型修改",
        "小修改",
        "小改",
        "小修复",
        "空指针",
        "小配置",
    )
    if any(term in normalized for term in research_terms):
        return WorkflowDecision(WorkflowComplexity.RESEARCH, "请求明确要求开展调查。")
    if any(term in normalized for term in complex_terms):
        return WorkflowDecision(WorkflowComplexity.COMPLEX, "请求存在跨模块或架构风险。")
    if any(term in normalized for term in tiny_terms):
        return WorkflowDecision(WorkflowComplexity.TINY, "预期修改明确且局部。")
    return WorkflowDecision(
        WorkflowComplexity.NORMAL, "没有更强证据时，常规工作流是安全的默认选择。"
    )


def upgrade_workflow(
    store: WorkspaceStore,
    requirement_id: str,
    target: WorkflowComplexity,
    reason: str,
) -> None:
    if not reason.strip():
        raise WorkspaceError("升级工作流必须提供有证据支持的原因")
    with store.locked(requirement_id):
        data = store.load(requirement_id)
        current = WorkflowComplexity(data["meta"]["workflow"])
        rank = {
            WorkflowComplexity.TINY: 0,
            WorkflowComplexity.NORMAL: 1,
            WorkflowComplexity.COMPLEX: 2,
        }
        if current is WorkflowComplexity.RESEARCH or target is WorkflowComplexity.RESEARCH:
            raise WorkspaceError("研究工作流必须在创建需求时明确选择")
        if rank[target] <= rank[current]:
            raise WorkspaceError(f"工作流只能升级：{current.value} -> {target.value}")
        history = list(data["meta"].get("workflow_history", []))
        history.append(
            {
                "from": current.value,
                "to": target.value,
                "reason": reason.strip(),
                "at": now_iso(),
            }
        )
        store.touch_meta(
            requirement_id,
            complexity=target.value,
            workflow=target.value,
            workflow_history=history,
        )
