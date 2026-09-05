"""临时真实 Git 验收；验证/审查 port 为明确测试夹具，不代表 OS 隔离能力。"""

from __future__ import annotations

import copy
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from workspace_orchestrator.integration.contracts import (
    IntegrationAuthorization,
    IntegrationError,
    RequirementReviewApproval,
)
from workspace_orchestrator.integration.git_workspace import LocalGitWorkspaceProvider
from workspace_orchestrator.integration.service import IntegrationService
from workspace_orchestrator.orchestration.contracts import (
    ExecutionPlan,
    TaskSpec,
    VerificationCommand,
    VerificationCommandResult,
    VerificationPlan,
    VerificationReceiptEnvelope,
    commands_fingerprint,
    fingerprint,
)

ENVIRONMENT = {"backend": "trusted-test-fixture"}
COMMANDS = (VerificationCommand("fixture", ("fixture-no-process",)),)


def git(path: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *argv], check=True, text=True, encoding="utf-8",
        capture_output=True,
    ).stdout.strip()


def evidence(
    plan: VerificationPlan, *, returncode: int = 0, timestamp: float | None = None,
) -> VerificationReceiptEnvelope:
    now = datetime.fromtimestamp(time.time() if timestamp is None else timestamp, UTC).isoformat()
    return VerificationReceiptEnvelope(
        "fixture-" + plan.plan_id, plan.plan_id, plan.requirement_id, plan.task_id,
        plan.candidate_sha, plan.candidate_tree, dict(plan.environment), plan.commands_fingerprint,
        tuple(VerificationCommandResult(command.command_id, returncode, "0" * 64, "0" * 64)
              for command in plan.commands),
        now, now, "trusted-test-fixture", "1",
    )


class FixtureVerifier:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_post = False
        self.fail_integration = False
        self.wait: tuple[threading.Event, threading.Event] | None = None
        self.review: FixtureReview | None = None
        self.clock: Callable[[], float] = time.time

    def execute(self, plan: VerificationPlan, *, workspace_path: Path) -> VerificationReceiptEnvelope:
        assert self.review is None or not self.review.guarded
        assert git(workspace_path, "rev-parse", "HEAD") == plan.candidate_sha
        self.calls.append(plan.task_id)
        if self.wait is not None and plan.task_id == "integration":
            entered, release = self.wait
            entered.set()
            assert release.wait(20)
        failed = ((self.fail_post and plan.task_id == "post-merge")
                  or (self.fail_integration and plan.task_id == "integration"))
        return evidence(plan, returncode=1 if failed else 0, timestamp=self.clock())


class FixtureReview:
    def __init__(self) -> None:
        self.passed = True
        self.revision = "current"
        self.calls = 0
        self.lock = threading.RLock()
        self.guarded = False
        self.clock: Callable[[], float] = time.time

    def review(
        self, requirement_id: str, snapshot_fingerprint: str,
        candidate_sha: str, candidate_tree: str,
    ) -> RequirementReviewApproval:
        self.calls += 1
        if not self.passed:
            raise IntegrationError("review_failed", "测试 Requirement Review 拒绝")
        now = datetime.fromtimestamp(self.clock(), UTC)
        return RequirementReviewApproval(
            requirement_id, snapshot_fingerprint, candidate_sha, candidate_tree,
            fingerprint(self.revision), now.isoformat(), (now + timedelta(minutes=5)).isoformat(),
            "trusted-test-fixture",
        )

    def revalidate(self, approval: RequirementReviewApproval) -> None:
        if not self.passed or approval.review_fingerprint != fingerprint(self.revision):
            raise IntegrationError("stale_review", "测试 Review 版本已变化")

    @contextmanager
    def guard(self, approval: RequirementReviewApproval) -> Iterator[None]:
        with self.lock:
            self.revalidate(approval)
            self.guarded = True
            try:
                yield
            finally:
                self.guarded = False


@dataclass
class Fixture:
    repo: Path
    remote: Path
    base: str
    workspaces: LocalGitWorkspaceProvider
    snapshots: dict[str, dict[str, Any]]
    verifier: FixtureVerifier
    reviewer: FixtureReview
    clock: Callable[[], float]

    def service(self, **kwargs: Any) -> IntegrationService:
        clock = kwargs.pop("clock", self.clock)
        return IntegrationService(
            self.repo, snapshot_reader=lambda requirement: copy.deepcopy(self.snapshots[requirement]),
            review_authority=self.reviewer, verifier=self.verifier,
            workspace_provider=self.workspaces, clock=clock, **kwargs,
        )

    def task(self, requirement: str, task_id: str, name: str, content: str) -> TaskSpec:
        lease = self.workspaces.ensure(requirement, task_id, base_sha=self.base)
        path = Path(lease.worktree)
        (path / name).write_bytes(content.encode("utf-8"))
        spec = TaskSpec(task_id, task_id, "临时测试任务", write_required=True,
                        worktree=str(path), branch=lease.branch)
        sha, tree = self.workspaces.capture_candidate(spec)
        plan = VerificationPlan("accept-" + task_id, requirement, task_id, sha, tree,
                                dict(ENVIRONMENT), COMMANDS, commands_fingerprint(COMMANDS))
        data = self.snapshots.setdefault(requirement, {"requirement_id": requirement, "nodes": {}})
        data["nodes"][task_id] = {
            "spec": spec.to_dict(), "status": "accepted", "active_attempt_id": None,
            "candidate_sha": sha, "candidate_tree": tree,
            "verification": {"status": "passed", "plan": plan.to_dict(),
                             "receipt": evidence(plan, timestamp=self.verifier.clock()).to_dict()},
        }
        tasks = tuple(TaskSpec.from_dict(node["spec"]) for node in data["nodes"].values())
        data["plan"] = ExecutionPlan("fixture-plan", requirement, "direct" if len(tasks) == 1 else "dag",
                                      tasks).to_dict()
        return spec

    def integrate(self, service: IntegrationService | None = None, request: str = "first") -> Any:
        return (service or self.service()).integrate("REQ-901", request, self.base, COMMANDS, ENVIRONMENT)


@pytest.fixture
def integration(tmp_path: Path) -> Fixture:
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@localhost")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "base.txt").write_bytes(b"base\n")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    provider = LocalGitWorkspaceProvider(repo, tmp_path / "state", tmp_path / "tasks")
    fixture_now = float(int(time.time()))
    clock = lambda: fixture_now
    fixture = Fixture(repo, remote, git(repo, "rev-parse", "HEAD"), provider, {},
                      FixtureVerifier(), FixtureReview(), clock)
    fixture.verifier.clock = fixture.reviewer.clock = clock
    fixture.verifier.review = fixture.reviewer
    fixture.task("REQ-901", "TASK-1", "one.txt", "one\n")
    return fixture


def test_native_merge_pass_and_same_request_exactly_once(integration: Fixture) -> None:
    integration.task("REQ-901", "TASK-2", "two.txt", "two\n")
    service = integration.service()
    first = integration.integrate(service)
    second = integration.integrate(integration.service())
    assert first == second
    assert first.status == "merged"
    assert git(integration.repo, "rev-parse", "HEAD") == first.merged_sha
    assert (integration.repo / "one.txt").read_text() == "one\n"
    assert (integration.repo / "two.txt").read_text() == "two\n"
    assert integration.verifier.calls == ["integration", "post-merge"]
    assert "completion_token" not in first.to_dict()
    assert not any("deploy" in key for key in first.to_dict())
    record = service.status("REQ-901", "first")
    auth = IntegrationAuthorization.from_dict(record["authorization"])
    assert auth.task_ids == ("TASK-1", "TASK-2")
    assert auth.candidate_sha == first.merged_sha
    assert integration.snapshots["REQ-901"]["nodes"]["TASK-1"]["status"] == "accepted"


@pytest.mark.parametrize("mutation", ["unaccepted", "missing", "old", "environment", "tree", "commands"])
def test_unaccepted_unverified_and_stale_task_evidence_rejected(integration: Fixture, mutation: str) -> None:
    node = integration.snapshots["REQ-901"]["nodes"]["TASK-1"]
    if mutation == "unaccepted":
        node["status"] = "candidate_complete"
    elif mutation == "missing":
        node["verification"]["status"] = "running"
    elif mutation == "old":
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        node["verification"]["receipt"].update(started_at=old, completed_at=old)
    elif mutation == "environment":
        node["verification"]["receipt"]["environment"] = {"backend": "other"}
    elif mutation == "tree":
        node["verification"]["receipt"]["candidate_tree"] = "1" * 40
    else:
        node["verification"]["receipt"]["commands_fingerprint"] = "1" * 64
    with pytest.raises((ValueError, RuntimeError)):
        integration.integrate()
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base
    assert not integration.verifier.calls
    assert integration.reviewer.calls == 0


def test_review_rejection_never_mints_authorization(integration: Fixture) -> None:
    integration.reviewer.passed = False
    service = integration.service()
    with pytest.raises(IntegrationError, match="Review"):
        integration.integrate(service)
    assert "authorization" not in service.status("REQ-901", "first")
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "ignored", "index"])
def test_dirty_main_is_never_overwritten(integration: Fixture, dirty: str) -> None:
    if dirty == "tracked":
        (integration.repo / "base.txt").write_text("USER EDIT", encoding="utf-8")
    elif dirty == "index":
        (integration.repo / "base.txt").write_text("STAGED USER EDIT", encoding="utf-8")
        git(integration.repo, "add", "base.txt")
    else:
        (integration.repo / "user.tmp").write_text("USER EDIT", encoding="utf-8")
        if dirty == "ignored":
            (integration.repo / ".git" / "info" / "exclude").write_text("user.tmp\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in integration.repo.iterdir() if path.is_file()}
    with pytest.raises((ValueError, RuntimeError)):
        integration.integrate()
    assert before == {path.name: path.read_bytes() for path in integration.repo.iterdir() if path.is_file()}
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base


def test_conflict_retains_task_branches_and_main(integration: Fixture) -> None:
    first = TaskSpec.from_dict(integration.snapshots["REQ-901"]["nodes"]["TASK-1"]["spec"])
    integration.task("REQ-901", "TASK-2", "one.txt", "incompatible\n")
    with pytest.raises(IntegrationError, match="冲突"):
        integration.integrate()
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base
    assert Path(first.worktree or "").exists()
    assert git(Path(first.worktree or ""), "symbolic-ref", "--short", "HEAD") == first.branch


def test_unexpected_main_and_request_reuse_rejected(integration: Fixture) -> None:
    service = integration.service()
    with pytest.raises((ValueError, RuntimeError)):
        service.integrate("REQ-901", "bad-base", "1" * 40, COMMANDS, ENVIRONMENT)
    integration.integrate(service)
    with pytest.raises(IntegrationError, match="不同参数"):
        service.integrate("REQ-901", "first", "1" * 40, COMMANDS, ENVIRONMENT)
    assert integration.verifier.calls == ["integration", "post-merge"]


def test_remote_advance_without_fetch_is_detected(integration: Fixture, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(integration.repo, "clone", "--branch", "main", str(integration.remote), str(other))
    git(other, "config", "user.name", "Fixture")
    git(other, "config", "user.email", "fixture@localhost")
    (other / "remote.txt").write_text("remote changed", encoding="utf-8")
    git(other, "add", "remote.txt")
    git(other, "commit", "-m", "remote")
    git(other, "push", "origin", "main")
    assert git(integration.repo, "rev-parse", "origin/main") == integration.base
    with pytest.raises((ValueError, RuntimeError)):
        integration.integrate()
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base


class Crash(BaseException):
    """模拟进程直接退出，普通错误处理不能将其伪装为已撤销。"""


@pytest.mark.parametrize("point", ["before_ref_update", "after_ref_update"])
def test_crash_around_ref_update_reconciles_once(integration: Fixture, point: str) -> None:
    def crash(name: str) -> None:
        if name == point:
            raise Crash()
    service = integration.service(failpoint=crash)
    with pytest.raises(Crash):
        integration.integrate(service)
    record = service.status("REQ-901", "first")
    expected = integration.base if point == "before_ref_update" else record["candidate_sha"]
    assert git(integration.repo, "rev-parse", "HEAD") == expected
    resumed = integration.service()
    receipt = resumed.reconcile("REQ-901", "first")
    assert receipt.status == "merged"
    assert resumed.reconcile("REQ-901", "first") == receipt
    assert integration.verifier.calls == ["integration", "post-merge"]
    assert git(integration.repo, "status", "--porcelain") == ""


def test_post_merge_failure_retains_recovery_receipt_without_completion(integration: Fixture) -> None:
    integration.verifier.fail_post = True
    receipt = integration.integrate()
    assert receipt.status == "recovery_required"
    assert git(integration.repo, "rev-parse", "HEAD") == receipt.merged_sha
    assert "CompletionToken" not in repr(receipt.to_dict())
    assert Path(integration.service().status("REQ-901", "first")["worktree"]).exists()
    assert integration.service().reconcile("REQ-901", "first") == receipt
    assert integration.verifier.calls == ["integration", "post-merge"]


def test_ref_drift_after_authorization_fails_closed(integration: Fixture) -> None:
    def drift(name: str) -> None:
        if name == "after_authorization":
            (integration.repo / "external.txt").write_text("external", encoding="utf-8")
            git(integration.repo, "add", "external.txt")
            git(integration.repo, "commit", "-m", "external")
    service = integration.service(failpoint=drift)
    with pytest.raises(IntegrationError, match="main"):
        integration.integrate(service)
    assert (integration.repo / "external.txt").read_text() == "external"
    assert integration.verifier.calls == ["integration"]


def test_review_revision_changed_after_authorization_fails_closed(integration: Fixture) -> None:
    def drift(name: str) -> None:
        if name == "after_authorization":
            integration.reviewer.revision = "changed"
    with pytest.raises(IntegrationError, match="Review"):
        integration.integrate(integration.service(failpoint=drift))
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base


def test_user_edit_between_ref_update_and_reconcile_is_preserved(integration: Fixture) -> None:
    def crash(name: str) -> None:
        if name == "after_ref_update":
            raise Crash()
    service = integration.service(failpoint=crash)
    with pytest.raises(Crash):
        integration.integrate(service)
    (integration.repo / "base.txt").write_text("USER AFTER CRASH", encoding="utf-8")
    receipt = integration.service().reconcile("REQ-901", "first")
    assert receipt.status == "recovery_required"
    assert (integration.repo / "base.txt").read_text() == "USER AFTER CRASH"
    assert integration.verifier.calls == ["integration"]


def test_identical_concurrent_requests_share_writer_and_receipt(integration: Fixture) -> None:
    entered, release = threading.Event(), threading.Event()
    integration.verifier.wait = (entered, release)
    results: list[Any] = []
    failures: list[Exception] = []
    def run() -> None:
        try:
            results.append(integration.integrate())
        except Exception as exc:  # noqa: BLE001 -- 主测试线程统一断言后台失败。
            failures.append(exc)
    first, second = threading.Thread(target=run), threading.Thread(target=run)
    first.start()
    assert entered.wait(20)
    second.start()
    time.sleep(0.05)
    assert integration.verifier.calls == ["integration"]
    release.set()
    first.join(20)
    second.join(20)
    assert not first.is_alive() and not second.is_alive()
    assert not failures
    assert len(results) == 2 and results[0] == results[1]
    assert integration.verifier.calls == ["integration", "post-merge"]


def test_unknown_post_verification_is_not_reexecuted(integration: Fixture) -> None:
    original = integration.verifier.execute
    def crash(plan: VerificationPlan, *, workspace_path: Path) -> VerificationReceiptEnvelope:
        if plan.task_id == "post-merge":
            raise Crash()
        return original(plan, workspace_path=workspace_path)
    integration.verifier.execute = crash  # type: ignore[method-assign]
    with pytest.raises(Crash):
        integration.integrate()
    receipt = integration.service().reconcile("REQ-901", "first")
    assert receipt.status == "recovery_required"
    assert "未知" in receipt.reason
    assert integration.verifier.calls == ["integration"]
    with pytest.raises(IntegrationError, match="未知"):
        integration.service().recover_post_merge("REQ-901", "first", "do-not-replay")


def test_unknown_contract_fields_survive_roundtrip() -> None:
    now = datetime.now(UTC)
    approval = RequirementReviewApproval("REQ-901", "1" * 64, "2" * 40, "3" * 40,
                                         "4" * 64, now.isoformat(),
                                         (now + timedelta(minutes=5)).isoformat(), "fixture")
    payload = approval.to_dict()
    payload["future"] = {"unchanged": True}
    assert RequirementReviewApproval.from_dict(payload).to_dict() == payload
    expired = replace(approval, issued_at=(now - timedelta(days=2)).isoformat(),
                      expires_at=(now - timedelta(days=1)).isoformat())
    assert expired.expires_at != approval.expires_at


def test_waiting_for_real_review_can_resume_same_candidate(integration: Fixture) -> None:
    original = integration.reviewer.review
    def waiting(
        requirement_id: str, snapshot_fingerprint: str, candidate_sha: str, candidate_tree: str,
    ) -> RequirementReviewApproval:
        raise IntegrationError("manual_approval_required", "等待真实用户审批")
    integration.reviewer.review = waiting  # type: ignore[method-assign]
    service = integration.service()
    with pytest.raises(IntegrationError, match="等待"):
        integration.integrate(service)
    assert service.status("REQ-901", "first")["state"] == "waiting_review"
    assert "authorization" not in service.status("REQ-901", "first")
    integration.reviewer.review = original  # type: ignore[method-assign]
    assert service.reconcile("REQ-901", "first").status == "merged"
    assert integration.verifier.calls == ["integration", "post-merge"]


def test_recovery_request_blocks_other_requirement_queue(integration: Fixture) -> None:
    integration.task("REQ-902", "TASK-OTHER", "other.txt", "other")
    def crash(name: str) -> None:
        if name == "before_ref_update":
            raise Crash()
    with pytest.raises(Crash):
        integration.integrate(integration.service(failpoint=crash))
    with pytest.raises(IntegrationError, match="另一 merge request"):
        integration.service().integrate("REQ-902", "other", integration.base, COMMANDS, ENVIRONMENT)
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base


def test_authorization_last_moment_task_change_is_rejected(integration: Fixture) -> None:
    def changed(name: str) -> None:
        if name == "before_ref_update":
            integration.snapshots["REQ-901"]["nodes"]["TASK-1"]["status"] = "blocked"
    with pytest.raises(IntegrationError, match="reconcile"):
        integration.integrate(integration.service(failpoint=changed))
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base


def test_explicit_control_roots_preserved_during_main_checkout(integration: Fixture) -> None:
    control = integration.repo / ".workspace"
    control.mkdir()
    (control / "user-state.json").write_text("preserve", encoding="utf-8")
    receipt = integration.integrate(integration.service(preserved_roots=(control,)))
    assert receipt.status == "merged"
    assert (control / "user-state.json").read_text() == "preserve"


def test_candidate_cannot_overwrite_explicit_control_root(integration: Fixture) -> None:
    control = integration.repo / ".workspace"
    control.mkdir()
    (control / "user-state.json").write_text("preserve", encoding="utf-8")
    task = TaskSpec.from_dict(integration.snapshots["REQ-901"]["nodes"]["TASK-1"]["spec"])
    path = Path(task.worktree or "")
    (path / ".workspace").mkdir()
    (path / ".workspace" / "user-state.json").write_text("overwrite", encoding="utf-8")
    sha, tree = integration.workspaces.capture_candidate(task)
    node = integration.snapshots["REQ-901"]["nodes"]["TASK-1"]
    plan = VerificationPlan("new-candidate", "REQ-901", "TASK-1", sha, tree, ENVIRONMENT,
                            COMMANDS, commands_fingerprint(COMMANDS))
    node.update(candidate_sha=sha, candidate_tree=tree,
                verification={"status": "passed", "plan": plan.to_dict(), "receipt": evidence(plan).to_dict()})
    with pytest.raises(IntegrationError):
        integration.integrate(integration.service(preserved_roots=(control,)))
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base
    assert (control / "user-state.json").read_text() == "preserve"


def test_final_ref_update_holds_review_guard_without_locking_verification(integration: Fixture) -> None:
    wait_budget = 60
    service = integration.service()
    request = {
        "requirement_id": "REQ-901", "request_id": "first",
        "expected_main_sha": integration.base,
        "commands": [item.to_dict() for item in COMMANDS], "environment": ENVIRONMENT,
    }
    key = service._key("REQ-901", "first")
    with service.git.writer():
        snapshot, tasks = service._accepted("REQ-901", integration.base, ENVIRONMENT)
        journal = {
            "schema_version": 1, "key": key, "request": request,
            "request_fingerprint": fingerprint(request), "state": "created",
            "snapshot_fingerprint": fingerprint(snapshot),
            "task_ids": [task.task_id for task in tasks], "created_at": service._now(),
        }
        service._save(journal)
        service._prepare(journal)
    publish = service.adapter.publish
    publication_ready, writer_ready = threading.Barrier(2), threading.Barrier(2)
    allow_publication = threading.Event()
    writer_attempted, writer_completed = threading.Event(), threading.Event()
    receipts: list[Any] = []
    errors: list[BaseException] = []

    def release_waiters() -> None:
        allow_publication.set()
        publication_ready.abort()
        writer_ready.abort()

    def wait_at(barrier: threading.Barrier, label: str) -> None:
        try:
            barrier.wait(wait_budget)
        except threading.BrokenBarrierError as exc:
            raise AssertionError(f"{label} 未在 {wait_budget} 秒内完成同步") from exc

    def held_publish(expected: str, candidate: str) -> None:
        try:
            assert integration.reviewer.guarded
            wait_at(publication_ready, "最终 ref 发布进入")
            if not allow_publication.wait(wait_budget):
                raise AssertionError(f"最终 ref 发布未在 {wait_budget} 秒内获准继续")
            assert not writer_completed.is_set()
            publish(expected, candidate)
        except BaseException:
            release_waiters()
            raise
        finally:
            allow_publication.set()

    def publish_and_verify() -> None:
        try:
            with service.git.writer():
                receipts.append(service._publish_and_verify(journal))
        except BaseException as exc:  # noqa: BLE001 -- 后台错误带回主测试线程集中断言。
            errors.append(exc)
            release_waiters()

    def edit_review() -> None:
        try:
            writer_attempted.set()
            wait_at(writer_ready, "审查写者启动")
            with integration.reviewer.lock:
                integration.reviewer.revision = "after-publication"
                writer_completed.set()
        except BaseException as exc:  # noqa: BLE001 -- 后台错误带回主测试线程集中断言。
            errors.append(exc)
            release_waiters()

    service.adapter.publish = held_publish  # type: ignore[method-assign]
    worker = threading.Thread(target=publish_and_verify)
    writer = threading.Thread(target=edit_review)
    worker.start()
    main_error: BaseException | None = None
    try:
        wait_at(publication_ready, "主线程等待最终 ref 发布")
        writer.start()
        wait_at(writer_ready, "主线程等待审查写者")
        assert writer_attempted.is_set()
        assert not writer_completed.is_set()
    except BaseException as exc:  # noqa: BLE001 -- 先解除后台阻塞，再优先报告原始错误。
        main_error = exc
        release_waiters()
    finally:
        allow_publication.set()
        for thread in (worker, writer):
            if thread.ident is not None:
                thread.join(wait_budget)
    if errors:
        raise AssertionError("后台并发线程失败") from errors[0]
    if worker.is_alive() or writer.is_alive():
        raise AssertionError("并发测试线程未在有限预算内退出")
    if main_error is not None:
        raise main_error
    assert not worker.is_alive() and not writer.is_alive()
    assert not errors
    assert receipts[0].status == "merged"
    assert writer_completed.is_set()
    assert integration.verifier.calls == ["integration", "post-merge"]


def test_review_guard_entry_failure_blocks_publication(integration: Fixture) -> None:
    @contextmanager
    def unavailable(approval: RequirementReviewApproval) -> Iterator[None]:
        raise IntegrationError("review_lock_unavailable", "真实 Review 事务不可用")
        yield  # pragma: no cover -- contextmanager 的生成器声明，失败路径永不进入。

    integration.reviewer.guard = unavailable  # type: ignore[method-assign]
    with pytest.raises(IntegrationError, match="reconcile"):
        integration.integrate()
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base
    assert integration.verifier.calls == ["integration"]


def test_main_without_checkout_uses_only_native_ref_update(integration: Fixture) -> None:
    git(integration.repo, "switch", "-c", "controller")
    receipt = integration.integrate()
    assert receipt.status == "merged"
    assert git(integration.repo, "rev-parse", "main") == receipt.merged_sha
    assert git(integration.repo, "rev-parse", "HEAD") == integration.base
    assert git(integration.repo, "symbolic-ref", "--short", "HEAD") == "controller"
    assert not (integration.repo / "one.txt").exists()


def test_stale_authorization_after_slow_final_remote_check_rejected(integration: Fixture) -> None:
    service = integration.service()
    assert_main = service.adapter.assert_main
    original_clock = service.clock

    def slow_remote(expected: str, *, remote: bool = True) -> Path | None:
        result = assert_main(expected, remote=remote)
        if integration.reviewer.guarded:
            frozen_future = original_clock() + 600
            service.clock = lambda: frozen_future
        return result

    service.adapter.assert_main = slow_remote  # type: ignore[method-assign]
    with pytest.raises(IntegrationError, match="reconcile"):
        integration.integrate(service)
    assert git(integration.repo, "rev-parse", "main") == integration.base
    assert integration.verifier.calls == ["integration"]


def test_new_request_cannot_remerge_already_merged_candidates(integration: Fixture) -> None:
    receipt = integration.integrate()
    # 原租约仍绑定旧基线。新 ID 不能把同一已接受候选重新包装成另一次 merge。
    with pytest.raises(IntegrationError):
        integration.service().integrate("REQ-901", "duplicate", receipt.merged_sha, COMMANDS, ENVIRONMENT)
    assert git(integration.repo, "rev-parse", "main") == receipt.merged_sha
    assert integration.verifier.calls == ["integration", "post-merge"]


def test_known_integration_failure_keeps_result_without_claiming_unknown(integration: Fixture) -> None:
    integration.verifier.fail_integration = True
    service = integration.service()
    with pytest.raises(ValueError):
        integration.integrate(service)
    journal = service.status("REQ-901", "first")
    assert journal["state"] == "rejected"
    assert journal["integration_verification"]["results"][0]["returncode"] == 1
    assert "authorization" not in journal
    assert git(integration.repo, "rev-parse", "main") == integration.base
    integration.verifier.fail_integration = False
    assert integration.integrate(service, "after-known-failure").status == "merged"


def test_unknown_integration_execution_is_not_blindly_replayed(integration: Fixture) -> None:
    def crash(plan: VerificationPlan, *, workspace_path: Path) -> VerificationReceiptEnvelope:
        raise Crash()
    integration.verifier.execute = crash  # type: ignore[method-assign]
    service = integration.service()
    with pytest.raises(Crash):
        integration.integrate(service)
    with pytest.raises(IntegrationError, match="未知"):
        service.reconcile("REQ-901", "first")
    with pytest.raises(IntegrationError, match="另一 merge request"):
        integration.integrate(service, "not-a-retry")
    assert git(integration.repo, "rev-parse", "main") == integration.base


def test_main_checkout_reuses_native_git_eol_conversion(integration: Fixture) -> None:
    integration.task("REQ-901", "TASK-ATTRIBUTES", ".gitattributes", "*.txt text eol=crlf\n")
    receipt = integration.integrate()
    assert receipt.status == "merged"
    assert (integration.repo / "one.txt").read_bytes().endswith(b"\r\n")
    assert git(integration.repo, "rev-parse", "main") == receipt.merged_sha


@pytest.mark.parametrize("offset", [600, 7200])
def test_expired_prepublication_request_recovers_or_releases_queue(integration: Fixture, offset: int) -> None:
    def crash(name: str) -> None:
        if name == "before_ref_update":
            raise Crash()
    interrupted = integration.service(failpoint=crash)
    with pytest.raises(Crash):
        integration.integrate(interrupted)
    before = interrupted.status("REQ-901", "first")
    assert not interrupted.adapter.publication_observed(before["candidate_sha"])
    future = float(int(time.time()) + offset)
    integration.reviewer.clock = integration.verifier.clock = lambda: future
    resumed = integration.service(clock=lambda: future)
    if offset == 600:
        result = resumed.reconcile("REQ-901", "first")
        assert result.status == "merged"
        after = resumed.status("REQ-901", "first")
        assert after["authorization"]["authorization_id"] != before["authorization"]["authorization_id"]
        assert after["authorization_history"][0]["authorization"] == before["authorization"]
        assert integration.verifier.calls == ["integration", "post-merge"]
    else:
        with pytest.raises(IntegrationError, match="过期"):
            resumed.reconcile("REQ-901", "first")
        assert resumed.status("REQ-901", "first")["state"] == "rejected"
        assert git(integration.repo, "rev-parse", "main") == integration.base
        integration.task("REQ-902", "NEW-TASK", "new.txt", "new")
        result = resumed.integrate("REQ-902", "after-rejection", integration.base, COMMANDS, ENVIRONMENT)
        assert result.status == "merged"


def test_post_failure_recovery_ids_keep_history_and_release_other_requirement(integration: Fixture) -> None:
    integration.verifier.fail_post = True
    service = integration.service()
    original = integration.integrate(service)
    failed_retry = service.recover_post_merge("REQ-901", "first", "still-failing")
    assert failed_retry.status == "recovery_required"
    assert service.recover_post_merge("REQ-901", "first", "still-failing") == failed_retry
    assert integration.verifier.calls == ["integration", "post-merge", "post-merge"]
    integration.verifier.fail_post = False
    fixed = service.recover_post_merge("REQ-901", "first", "fixed-condition")
    assert fixed.status == "merged"
    assert fixed.merged_sha == original.merged_sha
    assert fixed.receipt_id != original.receipt_id != failed_retry.receipt_id
    assert service.recover_post_merge("REQ-901", "first", "fixed-condition") == fixed
    assert service.recover_post_merge("REQ-901", "first", "still-failing") == failed_retry
    assert service.reconcile("REQ-901", "first") == fixed
    journal = service.status("REQ-901", "first")
    assert journal["receipt_history"] == [original.to_dict(), failed_retry.to_dict()]
    assert [attempt["execution_state"] for attempt in journal["post_merge_attempts"]] == ["returned"] * 3
    assert len({attempt["plan"]["plan_id"] for attempt in journal["post_merge_attempts"]}) == 3
    integration.base = fixed.merged_sha
    integration.task("REQ-902", "NEXT-TASK", "next.txt", "next")
    next_result = service.integrate("REQ-902", "next", integration.base, COMMANDS, ENVIRONMENT)
    assert next_result.status == "merged"
    assert integration.verifier.calls == ["integration", "post-merge", "post-merge", "post-merge",
                                           "integration", "post-merge"]


def test_post_recovery_preserves_user_edits_and_resumes_explicit_not_started(integration: Fixture) -> None:
    def crash(name: str) -> None:
        if name == "after_ref_update":
            raise Crash()
    service = integration.service(failpoint=crash)
    with pytest.raises(Crash):
        integration.integrate(service)
    original_content = (integration.repo / "base.txt").read_bytes()
    (integration.repo / "base.txt").write_bytes(b"USER CHANGES MUST STAY")
    resumed = integration.service()
    original = resumed.reconcile("REQ-901", "first")
    assert original.status == "recovery_required"
    failed = resumed.recover_post_merge("REQ-901", "first", "user-edit-still-present")
    assert failed.status == "recovery_required"
    assert (integration.repo / "base.txt").read_bytes() == b"USER CHANGES MUST STAY"
    assert integration.verifier.calls == ["integration"]
    journal = resumed.status("REQ-901", "first")
    assert all(attempt["execution_state"] == "not_started" for attempt in journal["post_merge_attempts"])
    # 仅 fixture 显式恢复它自己写的文件；产品的恢复路径没有删除、覆盖或回滚用户数据。
    (integration.repo / "base.txt").write_bytes(original_content)
    fixed = resumed.recover_post_merge("REQ-901", "first", "user-restored-file")
    assert fixed.status == "merged"
    assert integration.verifier.calls == ["integration", "post-merge"]


def test_published_marker_prevents_republishing_after_external_main_rewind(integration: Fixture) -> None:
    def crash(name: str) -> None:
        if name == "after_ref_update":
            raise Crash()
    service = integration.service(failpoint=crash)
    with pytest.raises(Crash):
        integration.integrate(service)
    journal = service.status("REQ-901", "first")
    candidate = journal["candidate_sha"]
    assert service.adapter.publication_observed(candidate)
    # 临时 fixture 模拟外部用户显式撤回 main；恢复不能再次推回候选。
    git(integration.repo, "update-ref", "refs/heads/main", integration.base, candidate)
    with pytest.raises(IntegrationError, match="reconcile"):
        integration.service().reconcile("REQ-901", "first")
    assert git(integration.repo, "rev-parse", "main") == integration.base
    assert integration.verifier.calls == ["integration"]


def test_recovery_after_returned_result_crash_does_not_execute_twice(integration: Fixture) -> None:
    integration.verifier.fail_post = True
    original = integration.integrate()
    integration.verifier.fail_post = False
    def crash(name: str) -> None:
        if name == "after_post_verification_returned":
            raise Crash()
    service = integration.service(failpoint=crash)
    with pytest.raises(Crash):
        service.recover_post_merge("REQ-901", "first", "recover-once")
    assert integration.verifier.calls == ["integration", "post-merge", "post-merge"]
    resumed = integration.service()
    fixed = resumed.recover_post_merge("REQ-901", "first", "recover-once")
    assert fixed.status == "merged"
    assert resumed.recover_post_merge("REQ-901", "first", "recover-once") == fixed
    assert integration.verifier.calls == ["integration", "post-merge", "post-merge"]
    assert resumed.status("REQ-901", "first")["receipt_history"] == [original.to_dict()]
