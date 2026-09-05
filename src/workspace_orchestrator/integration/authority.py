"""复用既有 Requirement Review Gate 和发布流程，不建立第二套审批系统。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from ..adapters.agent import CodexAgentProvider
from ..adapters.base import TaskProvider
from ..automation.runtime import AutomationRuntime
from ..orchestration.contracts import _sha, fingerprint
from ..orchestration.store import OrchestrationStore
from ..review import require_current_review_packet, review_requirement
from ..review_packet import build_review_packet, validate_review_packet
from ..workspace import WorkspaceError, WorkspaceStore
from .contracts import IntegrationError, RequirementReviewApproval
from .git_workspace import TrustedGit


class WorkspaceReviewAuthority:
    """自动/人工审查均绑定实际候选；构造端是可信 composition，不接请求自报 PASS。"""

    authority_id = "workspace-review-v1"

    def __init__(
        self, workspace: WorkspaceStore, task_provider: TaskProvider | None,
        *, clock: Callable[[], float] = time.time,
    ) -> None:
        self.workspace, self.provider, self.clock = workspace, task_provider, clock
        self.git = TrustedGit(workspace.project_root)

    def _git_context(self, snapshot: str, sha: str, tree: str) -> dict[str, Any]:
        _sha(sha, "candidate_sha")
        _sha(tree, "candidate_tree")
        _sha(snapshot, "snapshot_fingerprint", digest=True)
        actual_tree = self.git.run("rev-parse", "--verify", f"{sha}^{{tree}}")
        if actual_tree != tree:
            raise IntegrationError("stale_review", "Review 候选 tree 与实际 Git 对象不同")
        return {
            "branch": f"candidate:{sha}",
            "worktree": str(self.workspace.working_root),
            "commits": (sha,),
            "diff": f"候选提交：{sha}\n候选 tree：{tree}\n已验收 Task 快照：{snapshot}",
        }

    def _facts(
        self, requirement_id: str, snapshot: str, sha: str, tree: str,
        *, publish: bool,
    ) -> str:
        if self.workspace.load(requirement_id)["meta"].get("task_provider") and self.provider is None:
            raise IntegrationError("review_unavailable", "已配置 Task Provider 当前不可用")
        review = review_requirement(
            self.workspace, requirement_id, self.provider, transition=False,
        )
        if not review.passed:
            raise IntegrationError("review_rejected", "Requirement Review 未通过：" + "；".join(review.blockers))
        tasks = self.provider.list_tasks(requirement_id) if self.provider is not None else ()
        context = self._git_context(snapshot, sha, tree)
        packet = build_review_packet(self.workspace, requirement_id, tasks=tasks, git=context)
        blockers = validate_review_packet(packet)
        if blockers:
            raise IntegrationError("review_rejected", "；".join(blockers))
        meta = self.workspace.load(requirement_id)["meta"]
        manual = bool(meta.get("manual_test_required"))
        approval_fact: dict[str, Any] | None = None
        if manual:
            if self.provider is None:
                raise IntegrationError("review_unavailable", "人工集成验收需要可验证用户活动的 Task Provider")
            try:
                review_task = require_current_review_packet(
                    self.workspace, requirement_id, self.provider, packet.fingerprint,
                )
            except WorkspaceError as exc:
                if not publish:
                    raise IntegrationError("stale_review", str(exc)) from exc
                runtime = AutomationRuntime(self.workspace, CodexAgentProvider(), self.provider)
                errors, _ = runtime._publish_review_packet(
                    requirement_id, self.provider,
                    git_context=lambda: self._git_context(snapshot, sha, tree),
                )
                reason = "；".join(errors) if errors else "已发布当前候选的 V1 Review Packet，等待用户验收"
                raise IntegrationError("manual_approval_required", reason) from exc
            if review_task is None or review_task.status != "done":
                raise IntegrationError("manual_approval_required", "当前候选的 Review 卡尚未由用户批准")
            fact = self.provider.review_approval_fact(review_task.id)
            if fact is None or fact.actor_type != "user" or not fact.actor_id:
                raise IntegrationError("manual_approval_required", "缺少最后一次进入 done 的可靠用户活动")
            approval_fact = asdict(fact)
        return fingerprint({"packet": packet.fingerprint, "manual": manual, "approval": approval_fact})

    def review(
        self, requirement_id: str, snapshot_fingerprint: str,
        candidate_sha: str, candidate_tree: str,
    ) -> RequirementReviewApproval:
        with self.workspace.provider_locked(requirement_id):
            proof = self._facts(
                requirement_id, snapshot_fingerprint, candidate_sha, candidate_tree, publish=True,
            )
            now = self.clock()
            return RequirementReviewApproval(
                requirement_id, snapshot_fingerprint, candidate_sha, candidate_tree, proof,
                datetime.fromtimestamp(now, UTC).isoformat(),
                datetime.fromtimestamp(now + 300, UTC).isoformat(), self.authority_id,
            )

    def revalidate(self, approval: RequirementReviewApproval) -> None:
        approval.validate()
        now = self.clock()
        if (approval.authority_id != self.authority_id
                or not datetime.fromisoformat(approval.issued_at).timestamp() <= now
                < datetime.fromisoformat(approval.expires_at).timestamp()):
            raise IntegrationError("stale_review", "Requirement Review 授权来源或有效期失效")
        with self.workspace.provider_locked(approval.requirement_id):
            proof = self._facts(
                approval.requirement_id, approval.snapshot_fingerprint,
                approval.candidate_sha, approval.candidate_tree, publish=False,
            )
            if proof != approval.review_fingerprint:
                raise IntegrationError("stale_review", "Requirement Review 事实已变化，拒绝旧授权")

    @contextmanager
    def guard(self, approval: RequirementReviewApproval) -> Iterator[None]:
        """集成持有 Git writer 后使用；复用原锁固定最终 Review 和 Task 快照。"""
        with (self.workspace.provider_locked(approval.requirement_id),
              self.workspace.locked(approval.requirement_id)):
            ledger = OrchestrationStore(
                self.workspace.path_for(approval.requirement_id) / "orchestration" / "supervisor",
            )
            with ledger.transaction():
                self.revalidate(approval)
                yield
