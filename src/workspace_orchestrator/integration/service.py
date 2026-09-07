"""复用 Supervisor、Review 和 Verification 的最小本地 merge queue。"""

from __future__ import annotations

import copy
import math
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..orchestration.contracts import (
    ExecutionPlan,
    TaskSpec,
    VerificationCommand,
    VerificationPlan,
    VerificationReceiptEnvelope,
    commands_fingerprint,
    fingerprint,
)
from ..orchestration.ports import VerificationExecutorPort
from ..workspace import WorkspaceStore
from .contracts import (
    IntegrationAuthorization,
    IntegrationError,
    MergeReceipt,
    RequirementReviewApproval,
    RequirementReviewPort,
)
from .git_integration import GitIntegrationAdapter
from .git_workspace import LocalGitWorkspaceProvider, TrustedGit, _physical


class IntegrationService:
    """可信 composition 持有本对象；CLI 输入不能提供候选/验收/授权事实。

    所有 Requirement 共用原 Git writer 锁与 journal 根。锁协调控制面，不是
    恶意 Worker 的安全边界；Worker 的 OS 隔离仍必须保护 common_dir。
    """

    def __init__(
        self, repo_root: Path, *,
        snapshot_reader: Callable[[str], dict[str, Any]],
        review_authority: RequirementReviewPort,
        verifier: VerificationExecutorPort,
        workspace_provider: LocalGitWorkspaceProvider,
        main_branch: str = "main", remote: str = "origin",
        preserved_roots: tuple[Path, ...] = (),
        max_evidence_age_seconds: float = 3600,
        clock: Callable[[], float] = time.time,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if (isinstance(max_evidence_age_seconds, bool)
                or not math.isfinite(max_evidence_age_seconds) or max_evidence_age_seconds <= 0):
            raise IntegrationError("invalid_freshness", "证据有效期必须是正有限秒数")
        self.git = TrustedGit(repo_root)
        self.adapter = GitIntegrationAdapter(self.git, main_branch=main_branch, remote=remote,
                                              preserved_roots=preserved_roots)
        self.root = self.git.common_dir / "ai-dev-os-control" / "integration"
        self.snapshot_reader, self.review_authority = snapshot_reader, review_authority
        self.verifier, self.workspace_provider = verifier, workspace_provider
        self.max_age, self.clock, self.failpoint = max_evidence_age_seconds, clock, failpoint

    def integrate(
        self, requirement_id: str, request_id: str, expected_main_sha: str,
        commands: tuple[VerificationCommand, ...], environment: dict[str, str],
    ) -> MergeReceipt:
        key = self._key(requirement_id, request_id)
        request = {
            "requirement_id": requirement_id, "request_id": request_id,
            "expected_main_sha": expected_main_sha,
            "commands": [item.to_dict() for item in commands], "environment": environment,
        }
        commands_fingerprint(commands)
        if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", expected_main_sha):
            raise IntegrationError("invalid_main", "expected main 必须是完整 SHA")
        # 借用现有验证契约检查环境及命令，不新增一套校验器。
        VerificationPlan("validate-request", requirement_id, "integration", expected_main_sha,
                         expected_main_sha, dict(environment), commands, commands_fingerprint(commands))
        with self.git.writer():
            journal = self._read(key)
            if journal is not None:
                if fingerprint(journal["request"]) != fingerprint(request):
                    raise IntegrationError("request_conflict", "同一 request ID 不能换用不同参数")
            else:
                self._queue_available(key)
                snapshot, tasks = self._accepted(requirement_id, expected_main_sha, environment)
                self.adapter.assert_main(expected_main_sha)
                journal = {
                    "schema_version": 1, "key": key, "request": copy.deepcopy(request),
                    "request_fingerprint": fingerprint(request), "state": "created",
                    "snapshot_fingerprint": fingerprint(snapshot),
                    "task_ids": [task.task_id for task in tasks], "created_at": self._now(),
                }
                self._save(journal)
            return self._continue(journal)

    def reconcile(self, requirement_id: str, request_id: str) -> MergeReceipt:
        """只恢复原持久意图；未知验证不会被静默重放，用户文件永不回滚。"""
        with self.git.writer():
            journal = self._read(self._key(requirement_id, request_id))
            if journal is None:
                raise IntegrationError("request_missing", "没有可恢复的 merge request")
            return self._continue(journal)

    def recover_post_merge(
        self, requirement_id: str, request_id: str, recovery_id: str,
    ) -> MergeReceipt:
        """显式重验原已发布候选；同 recovery ID 幂等，未知执行不得重新发送。"""
        if (not isinstance(recovery_id, str) or not recovery_id.strip() or len(recovery_id) > 200
                or any(ord(char) < 32 for char in recovery_id)):
            raise IntegrationError("invalid_recovery", "recovery ID 必须是无控制字符的 1–200 字符串")
        with self.git.writer():
            journal = self._read(self._key(requirement_id, request_id))
            if journal is None:
                raise IntegrationError("request_missing", "没有此 merge request")
            attempts = journal.get("post_merge_attempts", [])
            same = [item for item in attempts if item.get("recovery_id") == recovery_id]
            if same:
                if "receipt" in same[0]:
                    return MergeReceipt.from_dict(same[0]["receipt"])
                return self._run_post_verification(journal, same[0])
            if not attempts or "receipt" not in journal:
                raise IntegrationError("recovery_not_ready", "先 reconcile 原发布操作，不能跳过未知结果")
            if journal["receipt"]["status"] == "merged":
                raise IntegrationError("already_merged", "该请求已成功恢复，不开启新的验证尝试")
            previous = attempts[-1]
            if previous["execution_state"] not in ("not_started", "returned"):
                raise IntegrationError("verification_unknown", "前次 post-merge 执行仍未知，拒绝盲目重放")
            self._require_published(journal)
            attempt = self._new_post_attempt(journal, recovery_id)
            return self._run_post_verification(journal, attempt)

    def status(self, requirement_id: str, request_id: str) -> dict[str, Any]:
        with self.git.writer():
            journal = self._read(self._key(requirement_id, request_id))
            if journal is None:
                raise IntegrationError("request_missing", "没有此 merge request")
            return copy.deepcopy(journal)

    def _continue(self, journal: dict[str, Any]) -> MergeReceipt:
        if "receipt" in journal:
            return MergeReceipt.from_dict(journal["receipt"])
        state = journal["state"]
        if state in ("post_verification_running", "post_verification_ready"):
            attempts = journal.get("post_merge_attempts", [])
            if not attempts:
                raise IntegrationError("verification_unknown", "旧 post-merge 意图缺少可证明的执行状态")
            return self._run_post_verification(journal, attempts[-1])
        if state == "integration_verification_running":
            raise IntegrationError("verification_unknown", "上次集成验证结果未知，保留现场等待受控处理")
        if state == "rejected":
            raise IntegrationError("request_rejected", journal["reason"])
        try:
            if state == "publishing":
                state = self._recover_publication(journal)
            if state not in ("publishing", "published"):
                self._prepare(journal)
            return self._publish_and_verify(journal)
        except Exception as exc:
            # publishing 已落盘后不能把 Git/磁盘/网络异常解释成未发生副作用。
            if journal["state"] in ("publishing", "published", "post_verification_ready", "post_verification_running"):
                raise IntegrationError("recovery_required", f"发布结果需要 reconcile：{exc}") from exc
            if journal["state"] == "integration_verification_running":
                raise IntegrationError("verification_unknown", f"验证未确认收敛，保留队列：{exc}") from exc
            if isinstance(exc, IntegrationError) and exc.code in ("manual_approval_required", "review_unavailable"):
                journal.update(state="waiting_review", reason=str(exc))
                self._save(journal)
                raise
            journal.update(state="rejected", reason=str(exc))
            self._save(journal)
            raise

    def _recover_publication(self, journal: dict[str, Any]) -> str:
        candidate = journal["candidate_sha"]
        if journal.get("publication_marker") != self.adapter.publication_ref(candidate):
            raise IntegrationError("publication_unknown", "旧发布意图没有原子识别标记，不能假定没有副作用")
        actual = self.git.resolve(self.adapter.main_ref)
        if self.adapter.publication_observed(candidate):
            if actual != candidate:
                raise IntegrationError("main_drift", "原发布已发生但 main 被外部改变，保留现场而不重复合并")
            return "publishing"
        # 原子标记不存在，说明本请求的 native ref transaction 没有成功提交。
        # 从 publishing 离开后再审查；陈旧/脏树等确定失败可进入 rejected 释放队列。
        journal.update(state="reauthorizing", publication_recovery={
            "observed_main": actual, "marker_absent": True, "observed_at": self._now(),
        })
        self._save(journal)
        if actual != journal["request"]["expected_main_sha"]:
            raise IntegrationError("main_drift", "未发生本次发布，但 main 已漂移，旧请求应终止")
        return "reauthorizing"

    def _prepare(self, journal: dict[str, Any]) -> None:
        request = journal["request"]
        requirement_id, expected = request["requirement_id"], request["expected_main_sha"]
        snapshot, tasks = self._accepted(requirement_id, expected, request["environment"])
        if fingerprint(snapshot) != journal["snapshot_fingerprint"]:
            raise IntegrationError("snapshot_changed", "Task/Requirement 当前版本与排队版本不一致")
        self.adapter.assert_main(expected)
        if "candidate_sha" not in journal:
            candidate, tree = self.adapter.build_candidate(
                expected, tuple(snapshot["nodes"][task.task_id]["candidate_sha"] for task in tasks),
                identity=journal["key"], created_at=journal["created_at"],
            )
            journal.update(candidate_sha=candidate, candidate_tree=tree,
                           integration_ref=f"refs/heads/integration/{requirement_id}/{journal['key']}",
                           worktree=str(self.root / "worktrees" / journal["key"]))
            self._save(journal)
        self.adapter.ensure_diagnostic(journal["candidate_sha"], journal["integration_ref"],
                                       Path(journal["worktree"]), initialize=not journal.get("diagnostic_ready", False))
        if not journal.get("diagnostic_ready"):
            journal["diagnostic_ready"] = True
            self._save(journal)
        if "integration_verification" not in journal:
            plan = self._plan(journal, "integration")
            started = self.clock()
            journal.update(state="integration_verification_running", integration_plan=plan.to_dict(),
                           integration_execution_started=started)
            self._save(journal)
            receipt = self.verifier.execute(plan, workspace_path=Path(journal["worktree"]))
            # 返回的真实失败结果也必须保存；未知执行仍由未返回的 running 意图保留。
            journal.update(state="integration_verification_returned", integration_verification=receipt.to_dict())
            self._save(journal)
        self._executed(VerificationReceiptEnvelope.from_dict(journal["integration_verification"]),
                       VerificationPlan.from_dict(journal["integration_plan"]),
                       journal["integration_execution_started"])
        journal["state"] = "verified"
        self._save(journal)
        approval = self.review_authority.review(
            requirement_id, journal["snapshot_fingerprint"], journal["candidate_sha"], journal["candidate_tree"],
        )
        self._approval(journal, approval)
        self.review_authority.revalidate(approval)
        history = journal.setdefault("authorization_history", [])
        if "authorization" in journal:
            history.append({"authorization": copy.deepcopy(journal["authorization"]),
                            "review": copy.deepcopy(journal["review"]), "superseded_at": self._now()})
        authorization = IntegrationAuthorization(
            f"authorization-{journal['key']}-{len(history) + 1}", requirement_id, request["request_id"],
            journal["request_fingerprint"], journal["snapshot_fingerprint"], tuple(journal["task_ids"]),
            expected, journal["candidate_sha"], journal["candidate_tree"], approval.review_fingerprint,
            journal["integration_verification"]["receipt_id"], self._now(), approval.expires_at,
        )
        journal.update(state="authorized", review=approval.to_dict(), authorization=authorization.to_dict())
        self._save(journal)
        self._point("after_authorization")

    def _publish_and_verify(self, journal: dict[str, Any]) -> MergeReceipt:
        request = journal["request"]
        expected, candidate = request["expected_main_sha"], journal["candidate_sha"]
        self._authorization(journal)
        actual = self.git.resolve(self.adapter.main_ref)
        if actual == expected:
            if journal["state"] == "published" or self.adapter.publication_observed(candidate):
                raise IntegrationError("main_drift", "已发布 main 被外部回退，拒绝再次合并")
            snapshot, _ = self._accepted(request["requirement_id"], expected, request["environment"])
            if fingerprint(snapshot) != journal["snapshot_fingerprint"]:
                raise IntegrationError("snapshot_changed", "授权后 Task 状态发生变化")
            approval = RequirementReviewApproval.from_dict(journal["review"])
            self._approval(journal, approval)
            self.review_authority.revalidate(approval)
            self._fresh_receipt(VerificationReceiptEnvelope.from_dict(journal["integration_verification"]),
                                VerificationPlan.from_dict(journal["integration_plan"]))
            self.git.assert_clean(Path(journal["worktree"]), revision=candidate)
            checkout = self.adapter.assert_main(expected)
            journal.update(state="publishing", main_checkout=str(checkout) if checkout else None,
                           publication_marker=self.adapter.publication_ref(candidate))
            self._save(journal)
            self._point("before_ref_update")
            # 复用既有 Requirement/Supervisor 状态锁，不能在最终 revalidate 与 CAS
            # 之间释放。长时间的验证执行始终在此 guard 外，避免阻塞用户事实写入。
            with self.review_authority.guard(approval):
                snapshot, _ = self._accepted(request["requirement_id"], expected, request["environment"])
                if fingerprint(snapshot) != journal["snapshot_fingerprint"]:
                    raise IntegrationError("snapshot_changed", "副作用前 Task 状态再次发生变化")
                self._approval(journal, approval)
                self.review_authority.revalidate(approval)
                self._fresh_receipt(VerificationReceiptEnvelope.from_dict(journal["integration_verification"]),
                                    VerificationPlan.from_dict(journal["integration_plan"]))
                # 最后一刻再读 main、工作树和实际远端；CAS 是外部 Git 写者的最后防线。
                if self.adapter.assert_main(expected) != checkout:
                    raise IntegrationError("main_worktree_drift", "副作用前 main checkout 已变化")
                if checkout is not None:
                    self.git.assert_preserved(checkout, candidate, preserved_roots=self.adapter.preserved_roots)
                self._approval(journal, approval)
                authorization = IntegrationAuthorization.from_dict(journal["authorization"])
                if not (datetime.fromisoformat(authorization.issued_at).timestamp() <= self.clock()
                        < datetime.fromisoformat(authorization.expires_at).timestamp()):
                    raise IntegrationError("stale_authorization", "最后检查期间 IntegrationAuthorization 已失效")
                self._fresh_receipt(VerificationReceiptEnvelope.from_dict(journal["integration_verification"]),
                                    VerificationPlan.from_dict(journal["integration_plan"]))
                self.adapter.publish(expected, candidate)
                self._point("after_ref_update")
                journal["state"] = "published"
                self._save(journal)
        elif (actual != candidate or journal["state"] not in ("publishing", "published")
              or not self.adapter.publication_observed(candidate)):
            raise IntegrationError("main_drift", "当前 main 不是原基线或本次可识别发布结果")
        journal["state"] = "published"
        self._save(journal)
        return self._run_post_verification(journal, self._new_post_attempt(journal, None))

    def _require_published(self, journal: dict[str, Any]) -> None:
        self._authorization(journal)
        if (not self.adapter.publication_observed(journal["candidate_sha"])
                or self.git.resolve(self.adapter.main_ref) != journal["candidate_sha"]):
            raise IntegrationError("main_drift", "当前 main 不是本请求已发布的唯一可识别候选")

    def _new_post_attempt(self, journal: dict[str, Any], recovery_id: str | None) -> dict[str, Any]:
        if "receipt" in journal:
            journal.setdefault("receipt_history", []).append(copy.deepcopy(journal.pop("receipt")))
        plan = self._plan(journal, "post-merge")
        if recovery_id is not None:
            payload = plan.to_dict()
            payload["plan_id"] += "-recovery-" + fingerprint(recovery_id)
            plan = VerificationPlan.from_dict(payload)
        attempt = {"recovery_id": recovery_id, "execution_state": "not_started",
                   "plan": plan.to_dict(), "created_at": self._now()}
        journal.setdefault("post_merge_attempts", []).append(attempt)
        journal.pop("post_merge_verification", None)
        journal.update(state="post_verification_ready", post_merge_plan=plan.to_dict())
        self._save(journal)
        return attempt

    def _run_post_verification(self, journal: dict[str, Any], attempt: dict[str, Any]) -> MergeReceipt:
        if "receipt" in attempt:
            return MergeReceipt.from_dict(attempt["receipt"])
        if attempt["execution_state"] in ("running", "unknown"):
            attempt["execution_state"] = "unknown"
            return self._receipt(journal, "上次 post-merge 执行结果未知，不能冒充成功或盲目重放", attempt)
        try:
            self._require_published(journal)
            expected, candidate = journal["request"]["expected_main_sha"], journal["candidate_sha"]
            self.adapter.reconcile_checkout(expected, candidate, journal["main_checkout"])
            self.adapter.assert_main(candidate)
            plan = VerificationPlan.from_dict(attempt["plan"])
            if attempt["execution_state"] == "not_started":
                self.git.assert_clean(Path(journal["worktree"]), revision=candidate)
                attempt.update(execution_state="running", started_at=self.clock())
                journal["state"] = "post_verification_running"
                self._save(journal)
                receipt = self.verifier.execute(plan, workspace_path=Path(journal["worktree"]))
                payload = receipt.to_dict()
                attempt.update(execution_state="returned", verification=payload, returned_at=self._now())
                journal["post_merge_verification"] = payload
                self._save(journal)
                self._point("after_post_verification_returned")
            receipt = VerificationReceiptEnvelope.from_dict(attempt["verification"])
            self._executed(receipt, plan, attempt["started_at"])
            self.adapter.assert_main(candidate)
            return self._receipt(journal, attempt=attempt)
        except Exception as exc:  # noqa: BLE001 -- post-merge 失败保留 main 和诊断树，不签完成/部署权限。
            if attempt["execution_state"] == "running":
                attempt["execution_state"] = "unknown"
            return self._receipt(journal, str(exc), attempt)

    def _accepted(
        self, requirement_id: str, expected: str, environment: dict[str, str],
    ) -> tuple[dict[str, Any], tuple[TaskSpec, ...]]:
        supplied = self.snapshot_reader(requirement_id)
        data = copy.deepcopy(supplied.get("data", supplied))
        if data.get("requirement_id") != requirement_id:
            raise IntegrationError("wrong_requirement", "Supervisor 快照不是当前 Requirement")
        plan = ExecutionPlan.from_dict(data["plan"])
        if plan.requirement_id != requirement_id:
            raise IntegrationError("wrong_requirement", "执行计划属于其他 Requirement")
        for task in plan.nodes:
            node = data["nodes"].get(task.task_id, {})
            if (node.get("status") != "accepted" or node.get("active_attempt_id") is not None
                    or node.get("spec") != task.to_dict()):
                raise IntegrationError("task_unaccepted", f"Task {task.task_id} 未在当前计划中受控验收")
            verification = node.get("verification", {})
            if verification.get("status") != "passed":
                raise IntegrationError("task_unverified", "Task 没有受控验证结果")
            verification_plan = VerificationPlan.from_dict(verification["plan"])
            receipt = VerificationReceiptEnvelope.from_dict(verification["receipt"])
            self._fresh_receipt(receipt, verification_plan)
            if (verification_plan.requirement_id != requirement_id
                    or verification_plan.task_id != task.task_id
                    or verification_plan.environment != environment
                    or (verification_plan.candidate_sha, verification_plan.candidate_tree)
                    != (node.get("candidate_sha"), node.get("candidate_tree"))):
                raise IntegrationError("stale_task", "Task 证据身份、环境或候选不匹配")
            lease = self.workspace_provider.get(requirement_id, task.task_id)
            if lease is None or lease.base_sha != expected:
                raise IntegrationError("unexpected_base", "Task lease 基线不是 expected main")
            if (lease.worktree, lease.branch) != (task.worktree, task.branch):
                raise IntegrationError("wrong_workspace", "Task 候选不属于当前 Requirement 的租约")
            if self.workspace_provider.read_candidate(task) != (receipt.candidate_sha, receipt.candidate_tree):
                raise IntegrationError("stale_candidate", "真实候选或工作树已变化")
            if not self.git.is_ancestor(expected, receipt.candidate_sha):
                raise IntegrationError("unexpected_base", "候选没有包含预期 main 基线")
        return data, plan.nodes

    def _plan(self, journal: dict[str, Any], stage: str) -> VerificationPlan:
        request = journal["request"]
        commands = tuple(VerificationCommand.from_dict(item) for item in request["commands"])
        return VerificationPlan(
            stage + "-" + journal["key"], request["requirement_id"], stage,
            journal["candidate_sha"], journal["candidate_tree"], dict(request["environment"]),
            commands, commands_fingerprint(commands),
        )

    def _executed(self, receipt: VerificationReceiptEnvelope, plan: VerificationPlan, started: float) -> None:
        self._fresh_receipt(receipt, plan)
        if datetime.fromisoformat(receipt.started_at).timestamp() < started:
            raise IntegrationError("stale_verification", "执行器返回了本次运行前的旧回执")

    def _fresh_receipt(self, receipt: VerificationReceiptEnvelope, plan: VerificationPlan) -> None:
        receipt.validate_for(plan)
        start = datetime.fromisoformat(receipt.started_at).timestamp()
        end = datetime.fromisoformat(receipt.completed_at).timestamp()
        now = self.clock()
        if not now - self.max_age <= start <= end <= now:
            raise IntegrationError("stale_verification", "验证证据过期或来自未来")

    def _approval(self, journal: dict[str, Any], approval: RequirementReviewApproval) -> None:
        approval.validate()
        if (approval.requirement_id != journal["request"]["requirement_id"]
                or approval.snapshot_fingerprint != journal["snapshot_fingerprint"]
                or approval.candidate_sha != journal["candidate_sha"]
                or approval.candidate_tree != journal["candidate_tree"]):
            raise IntegrationError("stale_review", "Requirement Review 没有绑定本次候选和当前版本")
        if not (datetime.fromisoformat(approval.issued_at).timestamp() <= self.clock()
                < datetime.fromisoformat(approval.expires_at).timestamp()):
            raise IntegrationError("stale_review", "Requirement Review 已过期或来自未来")

    @staticmethod
    def _authorization(journal: dict[str, Any]) -> None:
        """恢复只消费此前受控签发并完整绑定的授权，不接受任意 JSON 凭据拼装。"""
        authorization = IntegrationAuthorization.from_dict(journal["authorization"])
        request = journal["request"]
        expected = {
            "requirement_id": request["requirement_id"], "request_id": request["request_id"],
            "request_fingerprint": fingerprint(request), "snapshot_fingerprint": journal["snapshot_fingerprint"],
            "task_ids": tuple(journal["task_ids"]), "expected_main_sha": request["expected_main_sha"],
            "candidate_sha": journal["candidate_sha"], "candidate_tree": journal["candidate_tree"],
            "review_fingerprint": journal["review"]["review_fingerprint"],
            "verification_receipt_id": journal["integration_verification"]["receipt_id"],
            "expires_at": journal["review"]["expires_at"],
        }
        if any(getattr(authorization, name) != value for name, value in expected.items()):
            raise IntegrationError("invalid_authorization", "持久 IntegrationAuthorization 与原意图不匹配")

    def _receipt(
        self, journal: dict[str, Any], reason: str = "", attempt: dict[str, Any] | None = None,
    ) -> MergeReceipt:
        post = journal.get("post_merge_verification", {})
        suffix = ""
        if attempt is not None and attempt.get("recovery_id") is not None:
            suffix = "-recovery-" + fingerprint(attempt["recovery_id"])
        receipt = MergeReceipt(
            "merge-" + journal["key"] + suffix, journal["request"]["requirement_id"], journal["request"]["request_id"],
            "recovery_required" if reason else "merged", journal["request"]["expected_main_sha"],
            journal["candidate_sha"], journal["candidate_tree"], journal["integration_ref"],
            journal["authorization"]["authorization_id"], journal["integration_verification"]["receipt_id"],
            post.get("receipt_id"), self._now(), reason,
        )
        if attempt is not None:
            attempt["receipt"] = receipt.to_dict()
        journal.update(state="recovery_required" if reason else "completed", receipt=receipt.to_dict())
        self._save(journal)
        return receipt

    def _queue_available(self, key: str) -> None:
        if not self.root.exists():
            return
        for path in self.root.glob("*.json"):
            if path.stem != key:
                previous = self._read(path.stem)
                if previous is None:
                    continue
                if previous["state"] not in ("completed", "rejected"):
                    raise IntegrationError("merge_queue_busy", "另一 merge request 尚未收敛，请先恢复它")

    @staticmethod
    def _key(requirement_id: str, request_id: str) -> str:
        if not re.fullmatch(r"REQ-\d+", requirement_id):
            raise IntegrationError("invalid_requirement", "Requirement ID 必须是 REQ-数字")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise IntegrationError("invalid_request", "request ID 必须是 1–200 字符")
        return fingerprint({"requirement_id": requirement_id, "request_id": request_id})

    def _read(self, key: str) -> dict[str, Any] | None:
        self._check_root()
        path = self.root / (key + ".json")
        if not path.exists():
            return None
        _physical(path)
        value = WorkspaceStore.read_json(path)
        if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("key") != key:
            raise IntegrationError("invalid_journal", "合并 journal 格式不合法")
        if self._key(value["request"]["requirement_id"], value["request"]["request_id"]) != key:
            raise IntegrationError("invalid_journal", "journal 身份不匹配")
        return value

    def _save(self, journal: dict[str, Any]) -> None:
        self._check_root()
        WorkspaceStore.write_json(self.root / (journal["key"] + ".json"), journal)
        # 复用既有原子文件提交；POSIX 在 ref 副作用前还需提交目录项。
        if os.name != "nt":
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _check_root(self) -> None:
        _physical(self.root, missing=True)
        if self.root.exists() and not self.root.is_dir():
            raise IntegrationError("unsafe_journal", "控制面 journal 目录不安全")

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).isoformat()

    def _point(self, name: str) -> None:
        if self.failpoint is not None:
            self.failpoint(name)


IntegrationProvider = IntegrationService
