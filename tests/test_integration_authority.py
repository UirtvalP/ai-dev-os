"""沿用真实 V1 审查准备流程；所有 Git、Workspace 和 Provider 都是临时 fixture。"""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_automation_runtime import FakeTasks, _crashed_finalize_runtime, _git, _reviewable_runtime

from workspace_orchestrator.automation import runtime as runtime_module
from workspace_orchestrator.delivery_guard import (
    delivery_completion_guard,
    is_v2_delivery,
    mark_v2_delivery,
    require_delivery_completion,
)
from workspace_orchestrator.integration.authority import WorkspaceReviewAuthority
from workspace_orchestrator.integration.contracts import IntegrationError
from workspace_orchestrator.integration_composition import prepare_git_request
from workspace_orchestrator.models import ReviewApprovalFact
from workspace_orchestrator.orchestration.contracts import PlanningRequest, TaskSpec, fingerprint
from workspace_orchestrator.project_init import initialize_project
from workspace_orchestrator.review import (
    confirm_requirement_done,
    review_requirement,
    sync_requirement_review_outcome,
)
from workspace_orchestrator.review_packet import parse_review_packet_marker
from workspace_orchestrator.workspace import WorkspaceError


def _identity(root: Path) -> tuple[str, str]:
    def read(revision):
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", revision],
            capture_output=True, check=True, text=True, encoding="utf-8",
        ).stdout.strip()

    return read("HEAD"), read("HEAD^{tree}")


def _ready_authority(tmp_path: Path, *, manual: bool = False, provider=None):
    # 不重造 Review Gate：既有 fixture 只运行临时库中的三个受控 print 命令。
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path, provider or FakeTasks())
    result = runtime.finalize(requirement_id, completed=("完整临时实现和真实验证结果",))
    assert result.passed and not result.requirement_completed
    store.touch_meta(requirement_id, delivery_profile="v2", manual_test_required=manual)
    now = [1_788_600_000.0]
    authority = WorkspaceReviewAuthority(store, tasks, clock=lambda: now[0])
    sha, tree = _identity(tmp_path)
    snapshot = fingerprint({"tasks": ["TASK-001"], "candidate": sha})
    return store, requirement_id, tasks, runtime, authority, snapshot, sha, tree, now


def _publish_manual(authority, requirement_id, snapshot, sha, tree, store, tasks):
    with pytest.raises(IntegrationError) as caught:
        authority.review(requirement_id, snapshot, sha, tree)
    assert caught.value.code == "manual_approval_required"
    meta = store.load(requirement_id)["meta"]
    task = tasks.get_task(meta["requirement_review_task_id"])
    assert task.status == "in_review"
    assert parse_review_packet_marker(task.description) == (
        requirement_id, meta["review_packet_published_revision"],
        meta["review_packet_published_fingerprint"],
    )
    assert sha in task.description and tree in task.description and snapshot in task.description
    return task.id


def test_authority_auto_review_binds_real_git_and_never_completes_requirement(tmp_path):
    store, requirement_id, tasks, _, authority, snapshot, sha, tree, _ = _ready_authority(tmp_path)
    original_status = store.load(requirement_id)["meta"]["status"]
    approval = authority.review(requirement_id, snapshot, sha, tree)
    approval.validate()
    authority.revalidate(approval)
    assert (approval.candidate_sha, approval.candidate_tree) == _identity(tmp_path)
    assert approval.snapshot_fingerprint == snapshot
    assert approval.authority_id == "workspace-review-v1"
    assert store.load(requirement_id)["meta"]["status"] == original_status != "done"
    assert tasks.get_task("TASK-001").status == "in_review"


def test_authority_refuses_wrong_tree_for_real_candidate(tmp_path):
    _, requirement_id, _, _, authority, snapshot, sha, _, _ = _ready_authority(tmp_path)
    with pytest.raises(IntegrationError) as caught:
        authority.review(requirement_id, snapshot, sha, "f" * 40)
    assert caught.value.code == "stale_review"


@pytest.mark.parametrize("blocker", ["acceptance", "intent", "verification", "task"])
def test_authority_preserves_original_v1_review_blockers(tmp_path, blocker):
    store, requirement_id, tasks, _, authority, snapshot, sha, tree, _ = _ready_authority(tmp_path)
    data = store.load(requirement_id)
    if blocker == "acceptance":
        store.write_text(data["path"] / "requirement.md", data["requirement"].replace("- [x]", "- [ ]"))
    elif blocker == "intent":
        store.write_text(data["path"] / "intent.md", data["intent"].replace("：PASS", "：PARTIAL"))
    elif blocker == "verification":
        store.write_text(data["path"] / "verification.md", data["verification"].replace("状态：PASS", "状态：FAIL"))
    else:
        tasks.update_status("TASK-001", "in_progress")
    with pytest.raises(IntegrationError) as caught:
        authority.review(requirement_id, snapshot, sha, tree)
    assert caught.value.code == "review_rejected"
    assert store.load(requirement_id)["meta"]["status"] != "done"


def test_configured_provider_unavailable_cannot_be_treated_as_local_approval(tmp_path):
    store, requirement_id, _, _, _, snapshot, sha, tree, _ = _ready_authority(tmp_path)
    authority = WorkspaceReviewAuthority(store, None)
    with pytest.raises(IntegrationError) as caught:
        authority.review(requirement_id, snapshot, sha, tree)
    assert caught.value.code == "review_unavailable"


def test_manual_review_republishes_candidate_packet_then_consumes_reliable_user_fact(tmp_path):
    store, requirement_id, tasks, _, authority, snapshot, sha, tree, _ = _ready_authority(
        tmp_path, manual=True,
    )
    before = store.load(requirement_id)["meta"]["review_packet_published_revision"]
    review_id = _publish_manual(authority, requirement_id, snapshot, sha, tree, store, tasks)
    assert store.load(requirement_id)["meta"]["review_packet_published_revision"] == before + 1
    publications = len(tasks.review_publications)
    with pytest.raises(IntegrationError) as caught:
        authority.review(requirement_id, snapshot, sha, tree)
    assert caught.value.code == "manual_approval_required"
    assert len(tasks.review_publications) == publications
    tasks.update_status(review_id, "done")
    approval = authority.review(requirement_id, snapshot, sha, tree)
    authority.revalidate(approval)
    assert tasks.get_task(review_id).status == "done"
    assert store.load(requirement_id)["meta"]["status"] == "in_review"


@pytest.mark.parametrize("actor", [None, "agent", "unknown"])
def test_done_review_card_without_reliable_user_fact_is_not_authority(tmp_path, actor):
    store, requirement_id, tasks, _, authority, snapshot, sha, tree, _ = _ready_authority(
        tmp_path, manual=True,
    )
    review_id = _publish_manual(authority, requirement_id, snapshot, sha, tree, store, tasks)
    tasks.update_status(review_id, "done")
    tasks.approval_actors[review_id] = actor
    with pytest.raises(IntegrationError) as caught:
        authority.review(requirement_id, snapshot, sha, tree)
    assert caught.value.code == "manual_approval_required"
    assert store.load(requirement_id)["meta"]["status"] != "done"


def test_user_actor_without_identity_is_not_a_reliable_approval(tmp_path, monkeypatch):
    store, requirement_id, tasks, _, authority, snapshot, sha, tree, _ = _ready_authority(
        tmp_path, manual=True,
    )
    review_id = _publish_manual(authority, requirement_id, snapshot, sha, tree, store, tasks)
    tasks.update_status(review_id, "done")
    fact = ReviewApprovalFact("event", "user", "", "unknown", "2026-09-06T00:00:00Z")
    monkeypatch.setattr(tasks, "review_approval_fact", lambda _: fact)
    with pytest.raises(IntegrationError) as caught:
        authority.review(requirement_id, snapshot, sha, tree)
    assert caught.value.code == "manual_approval_required"


def test_candidate_change_invalidates_manual_packet_and_old_approval(tmp_path):
    store, requirement_id, tasks, _, authority, snapshot, sha, tree, _ = _ready_authority(
        tmp_path, manual=True,
    )
    review_id = _publish_manual(authority, requirement_id, snapshot, sha, tree, store, tasks)
    tasks.update_status(review_id, "done")
    approval = authority.review(requirement_id, snapshot, sha, tree)
    (tmp_path / "README.md").write_text("next candidate\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "new candidate")
    next_sha, next_tree = _identity(tmp_path)
    previous_revision = store.load(requirement_id)["meta"]["review_packet_published_revision"]
    with pytest.raises(IntegrationError) as caught:
        authority.revalidate(replace(approval, candidate_sha=next_sha, candidate_tree=next_tree))
    assert caught.value.code == "stale_review"
    _publish_manual(authority, requirement_id, snapshot, next_sha, next_tree, store, tasks)
    assert store.load(requirement_id)["meta"]["review_packet_published_revision"] == previous_revision + 1
    assert tasks.get_task(review_id).status == "in_review"
    with pytest.raises(IntegrationError) as caught:
        authority.revalidate(approval)
    assert caught.value.code == "stale_review"


@pytest.mark.parametrize("mutation", ["expired", "before-issued", "authority", "snapshot", "proof"])
def test_authority_rejects_expired_wrong_source_and_changed_binding(tmp_path, mutation):
    _, requirement_id, _, _, authority, snapshot, sha, tree, now = _ready_authority(tmp_path)
    approval = authority.review(requirement_id, snapshot, sha, tree)
    if mutation == "expired":
        now[0] += 300
    elif mutation == "before-issued":
        now[0] -= 1
    elif mutation == "authority":
        approval = replace(approval, authority_id="worker-self-report")
    elif mutation == "snapshot":
        approval = replace(approval, snapshot_fingerprint="f" * 64)
    else:
        approval = replace(approval, review_fingerprint="f" * 64)
    with pytest.raises(IntegrationError) as caught:
        authority.revalidate(approval)
    assert caught.value.code == "stale_review"


def test_changed_review_source_facts_invalidate_previously_issued_approval(tmp_path):
    store, requirement_id, _, _, authority, snapshot, sha, tree, _ = _ready_authority(tmp_path)
    approval = authority.review(requirement_id, snapshot, sha, tree)
    data = store.load(requirement_id)
    store.write_text(data["path"] / "requirement.md", data["requirement"].replace(
        "Review Packet", "Review Packet with changed goal",
    ))
    with pytest.raises(IntegrationError) as caught:
        authority.revalidate(approval)
    assert caught.value.code == "stale_review"


def test_changed_reliable_approval_activity_invalidates_issued_proof(tmp_path, monkeypatch):
    store, requirement_id, tasks, _, authority, snapshot, sha, tree, _ = _ready_authority(
        tmp_path, manual=True,
    )
    review_id = _publish_manual(authority, requirement_id, snapshot, sha, tree, store, tasks)
    tasks.update_status(review_id, "done")
    approval = authority.review(requirement_id, snapshot, sha, tree)
    original_fact = tasks.review_approval_fact(review_id)
    monkeypatch.setattr(tasks, "review_approval_fact", lambda _: replace(
        original_fact, activity_id="different-last-done-event",
    ))
    with pytest.raises(IntegrationError) as caught:
        authority.revalidate(approval)
    assert caught.value.code == "stale_review"


@pytest.mark.parametrize("entrypoint", ["core-confirm", "runtime-confirm", "finalize", "review-sync"])
def test_v2_legacy_completion_entrypoints_cannot_complete_requirement(tmp_path, entrypoint):
    store, requirement_id, tasks, runtime, _, _, _, _, _ = _ready_authority(tmp_path)
    before_tasks = tasks.list_tasks(requirement_id)
    if entrypoint == "core-confirm":
        with pytest.raises(WorkspaceError, match="CompletionToken"):
            confirm_requirement_done(store, requirement_id, user_confirmed=True, task_provider=tasks)
    elif entrypoint == "runtime-confirm":
        with pytest.raises(WorkspaceError, match="CompletionToken"):
            runtime.confirm(requirement_id, user_confirmed=True)
    elif entrypoint == "finalize":
        result = runtime.finalize(requirement_id)
        assert not result.passed and not result.requirement_completed
        assert any("CompletionToken" in item for item in result.blockers)
    else:
        review_id = store.load(requirement_id)["meta"]["requirement_review_task_id"]
        tasks.update_status(review_id, "done")
        before_tasks = tasks.list_tasks(requirement_id)
        assert sync_requirement_review_outcome(store, requirement_id, tasks) is None
        assert runtime.sync_reviews(requirement_id) == ()
    assert store.load(requirement_id)["meta"]["status"] == "in_review"
    assert tasks.list_tasks(requirement_id) == before_tasks


def test_v2_stop_hook_and_pending_finalize_recovery_cannot_mark_tasks_done(tmp_path):
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)
    initialize_project(tmp_path)
    store.touch_meta(
        requirement_id, delivery_profile="v2", manual_test_required=False,
        pending_auto_completion={"self_reported": True},
        completion_token={"passed": True}, merge_receipt={"status": "merged"},
    )
    original = store.load(requirement_id)
    task_states = tasks.list_tasks(requirement_id)
    result = runtime.auto_finish_pushed_thread()
    assert not result.completed and "CompletionToken" in result.reason
    blocker = runtime._recover_pending_auto_completion(requirement_id, "packet-thread", ("TASK-001",), tasks)
    assert "CompletionToken" in blocker
    assert store.load(requirement_id)["meta"]["status"] == original["meta"]["status"]
    assert store.load(requirement_id)["sessions"] == original["sessions"]
    assert tasks.list_tasks(requirement_id) == task_states


@pytest.mark.parametrize("manual", [False, True])
def test_v1_finalize_and_confirm_keep_existing_completion_behavior(tmp_path, manual):
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)
    store.touch_meta(requirement_id, manual_test_required=manual)
    assert not is_v2_delivery(store, requirement_id)
    require_delivery_completion(store, requirement_id)
    result = runtime.finalize(requirement_id, completed=("V1 兼容验证",))
    assert result.passed
    if manual:
        assert not result.requirement_completed
        runtime.confirm(requirement_id, user_confirmed=True)
    else:
        assert result.requirement_completed
        assert tasks.get_task("TASK-001").status == "done"
    assert store.load(requirement_id)["meta"]["status"] == "done"


def _prepare_during_legacy_work(store, requirement_id):
    # 真实 prepare、不同原始 Task；只在独立临时仓库创建 main ref。
    sha, _ = _identity(store.project_root)
    _git(store.project_root, "update-ref", "refs/heads/main", sha)
    return prepare_git_request(store, PlanningRequest(requirement_id, "继续 V2 交付", (
        TaskSpec("V2-WORK", "V2", "新一批实现", write_required=True),
    )), expected_main_sha=sha)


def test_v2_prepare_during_v1_finalize_verification_prevents_completion(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    store, requirement_id, tasks, runtime = _reviewable_runtime(repo)
    store.touch_meta(requirement_id, manual_test_required=False)
    original = runtime_module.run_known_verifications

    def verification(root):
        result = original(root)
        _prepare_during_legacy_work(store, requirement_id)
        return result

    monkeypatch.setattr(runtime_module, "run_known_verifications", verification)
    result = runtime.finalize(requirement_id)
    assert not result.passed and not result.requirement_completed
    assert any("CompletionToken" in item for item in result.blockers)
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"
    assert tasks.get_task("TASK-001").status != "done"


@pytest.mark.parametrize("window", ["pending-verification", "stop-before-complete"])
def test_v2_prepare_during_legacy_recovery_or_stop_cannot_complete(tmp_path, monkeypatch, window):
    repo = tmp_path / "repo"
    repo.mkdir()
    store, requirement_id, tasks, runtime, archived = _crashed_finalize_runtime(repo, monkeypatch)
    tasks.update_status("TASK-001", "in_review")
    if window == "pending-verification":
        original = runtime_module.run_known_verifications

        def verification(root):
            result = original(root)
            _prepare_during_legacy_work(store, requirement_id)
            return result

        monkeypatch.setattr(runtime_module, "run_known_verifications", verification)
    else:
        store.touch_meta(requirement_id, pending_auto_completion=None)
        original_recovery = runtime._recover_pending_auto_completion

        def recover(*args):
            result = original_recovery(*args)
            assert result is None
            _prepare_during_legacy_work(store, requirement_id)
            return result

        monkeypatch.setattr(runtime, "_recover_pending_auto_completion", recover)
    result = runtime.auto_finish_pushed_thread()
    assert not result.completed and "CompletionToken" in result.reason
    assert tasks.get_task("TASK-001").status == "in_review"
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"
    assert archived == []


def test_v2_admission_and_old_completion_share_short_workspace_boundary(tmp_path):
    store, requirement_id, _, _ = _reviewable_runtime(tmp_path)
    entering = threading.Event()

    def admit():
        entering.set()
        mark_v2_delivery(store, requirement_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with delivery_completion_guard(store, requirement_id):
            pending = executor.submit(admit)
            assert entering.wait(3)
            assert not pending.done()
            store.touch_meta(requirement_id, status="done")
        with pytest.raises(WorkspaceError, match="已完成"):
            pending.result(timeout=3)
    assert not is_v2_delivery(store, requirement_id)


@pytest.mark.parametrize("blocked", [False, True])
def test_readonly_review_keeps_success_and_invalidates_only_real_blockers(tmp_path, blocked):
    store, requirement_id, tasks, _, _, _, _, _, _ = _ready_authority(tmp_path)
    if blocked:
        tasks.update_status("TASK-001", "in_progress")
    result = review_requirement(store, requirement_id, tasks, transition=False)
    assert result.passed is not blocked
    assert store.load(requirement_id)["meta"]["status"] == ("in_progress" if blocked else "in_review")


@pytest.mark.parametrize("offline", [False, True])
def test_v2_review_feedback_and_pending_reopen_preserve_v1_workflow(tmp_path, monkeypatch, offline):
    from workspace_orchestrator.adapters.task import TaskProviderError

    store, requirement_id, tasks, runtime, authority, snapshot, sha, tree, _ = _ready_authority(
        tmp_path, manual=True,
    )
    review_id = _publish_manual(authority, requirement_id, snapshot, sha, tree, store, tasks)
    tasks.update_status(review_id, "done")
    approval = authority.review(requirement_id, snapshot, sha, tree)
    tasks.add_comment(review_id, "请修复退回反馈，并保持候选重新验收。")
    tasks.update_status(review_id, "in_progress")
    original = tasks.update_status

    def failing(task_id, status):
        if task_id == "TASK-001" and status == "in_progress":
            raise TaskProviderError("fixture offline reopen")
        return original(task_id, status)

    if offline:
        monkeypatch.setattr(tasks, "update_status", failing)
    # Hook 提供的是 V1 checkout 指纹；V2 反馈只校验当前发布卡身份，不用它批准合并。
    runtime.sync_reviews(requirement_id)
    assert "请修复退回反馈" in store.load(requirement_id)["state"]
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"
    if offline:
        assert store.load(requirement_id)["meta"]["pending_task_review_reopen"] is True
        monkeypatch.setattr(tasks, "update_status", original)
        assert runtime.sync_reviews(requirement_id)
    assert tasks.get_task("TASK-001").status == "in_progress"
    with pytest.raises(IntegrationError, match="Review"):
        authority.revalidate(approval)
