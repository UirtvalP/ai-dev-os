from pathlib import Path

import pytest

from workspace_orchestrator.cli import main
from workspace_orchestrator.intent import IntentStatus, review_intent
from workspace_orchestrator.models import Task, WorkflowComplexity
from workspace_orchestrator.review import confirm_requirement_done, review_requirement
from workspace_orchestrator.workflow import route_workflow, upgrade_workflow
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def mark_intent_review_pass(store: WorkspaceStore, requirement_id: str) -> None:
    data = store.load(requirement_id)
    intent = data["intent"].replace("：PARTIAL", "：PASS")
    store.write_text(data["path"] / "intent.md", intent)


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("Fix a typo in the login copy", WorkflowComplexity.TINY),
        ("Fix a bug in login validation", WorkflowComplexity.TINY),
        ("小型修改：修复登录校验 Bug", WorkflowComplexity.TINY),
        ("Tweak one config value", WorkflowComplexity.TINY),
        ("Add a login endpoint", WorkflowComplexity.NORMAL),
        ("Migrate the database schema", WorkflowComplexity.COMPLEX),
        ("Research an unknown performance issue", WorkflowComplexity.RESEARCH),
    ],
)
def test_route_workflow_uses_lightest_evidenced_path(
    request_text: str, expected: WorkflowComplexity
) -> None:
    assert route_workflow(request_text).complexity is expected


def test_workflow_can_upgrade_but_not_downgrade(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Tiny fix", complexity=WorkflowComplexity.TINY)

    upgrade_workflow(store, requirement_id, WorkflowComplexity.NORMAL, "It affects two modules")

    meta = store.load(requirement_id)["meta"]
    assert meta["workflow"] == "normal"
    assert meta["workflow_history"][0]["reason"] == "It affects two modules"
    with pytest.raises(WorkspaceError, match="只能升级"):
        upgrade_workflow(store, requirement_id, WorkflowComplexity.TINY, "Try to downgrade")


def test_review_blocks_until_acceptance_and_verification_pass(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo", acceptance=["Feature works"], task_provider=None)

    blocked = review_requirement(store, requirement_id)

    assert blocked.passed is False
    assert blocked.intent_status is IntentStatus.PARTIAL
    assert store.load(requirement_id)["meta"]["status"] == "draft"
    assert any("意图一致性状态为 PARTIAL" in item for item in blocked.blockers)
    assert any("验收标准" in item for item in blocked.blockers)
    assert any("验证" in item for item in blocked.blockers)


def test_review_enters_in_review_but_never_done(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo", acceptance=["Feature works"], task_provider=None)
    data = store.load(requirement_id)
    requirement = data["requirement"].replace("- [ ] Feature works", "- [x] Feature works")
    verification = data["verification"].replace("状态：TODO", "状态：PASS")
    store.write_text(data["path"] / "requirement.md", requirement)
    store.write_text(data["path"] / "verification.md", verification)
    mark_intent_review_pass(store, requirement_id)

    result = review_requirement(store, requirement_id)

    assert result.passed is True
    assert result.intent_status is IntentStatus.PASS
    assert store.load(requirement_id)["meta"]["status"] == "in_review"


def test_review_requires_explicit_user_confirmation_before_done(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo", acceptance=["Feature works"], task_provider=None)
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "requirement.md",
        data["requirement"].replace("- [ ] Feature works", "- [x] Feature works"),
    )
    store.write_text(
        data["path"] / "verification.md",
        data["verification"].replace("状态：TODO", "状态：PASS"),
    )
    mark_intent_review_pass(store, requirement_id)
    assert review_requirement(store, requirement_id).passed is True

    with pytest.raises(WorkspaceError, match="明确确认"):
        confirm_requirement_done(store, requirement_id, user_confirmed=False)
    assert store.load(requirement_id)["meta"]["status"] == "in_review"

    confirm_requirement_done(store, requirement_id, user_confirmed=True)
    assert store.load(requirement_id)["meta"]["status"] == "done"


def test_core_confirm_rejects_configured_provider_without_current_packet(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Provider confirm", task_provider="dashi")
    store.touch_meta(requirement_id, status="in_review")

    with pytest.raises(WorkspaceError, match="无法验证 Review Packet"):
        confirm_requirement_done(store, requirement_id, user_confirmed=True)

    assert store.load(requirement_id)["meta"]["status"] == "in_review"


def test_review_reopens_stale_in_review_when_acceptance_changes(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Stale review", acceptance=["Feature works"], task_provider=None)
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "requirement.md",
        data["requirement"].replace("- [ ] Feature works", "- [x] Feature works"),
    )
    store.write_text(
        data["path"] / "verification.md",
        data["verification"].replace("状态：TODO", "状态：PASS"),
    )
    mark_intent_review_pass(store, requirement_id)
    assert review_requirement(store, requirement_id).passed is True

    current = store.load(requirement_id)
    store.write_text(
        current["path"] / "requirement.md",
        current["requirement"].replace("- [x] Feature works", "- [ ] Feature works"),
    )
    result = review_requirement(store, requirement_id)

    assert result.passed is False
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"


def test_done_requirement_cannot_be_reopened_by_review(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Already done", task_provider=None)
    store.touch_meta(requirement_id, status="done")

    result = review_requirement(store, requirement_id)

    assert result.passed is False
    assert result.blockers == ("Requirement 已完成，不能重新进入审查",)
    assert store.load(requirement_id)["meta"]["status"] == "done"


def test_current_id_ignores_requirements_waiting_for_user_review(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    reviewed = store.create("Reviewed")
    active = store.create("Active")
    store.touch_meta(reviewed, status="in_review")
    store.touch_meta(active, status="in_progress")

    assert store.current_id() == active


def test_request_changes_cli_requires_feedback_and_reopens_requirement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Needs changes", task_provider=None)
    store.touch_meta(requirement_id, status="in_review")

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "request-changes",
                requirement_id,
                "--feedback",
                "请补充边界测试。",
            ]
        )
        == 0
    )

    assert "已恢复 in_progress" in capsys.readouterr().out
    data = store.load(requirement_id)
    assert data["meta"]["status"] == "in_progress"
    assert "请补充边界测试。" in data["state"]


def test_review_checks_configured_task_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo", acceptance=["Feature works"], task_provider=None)
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "requirement.md",
        data["requirement"].replace("- [ ] Feature works", "- [x] Feature works"),
    )
    store.write_text(
        data["path"] / "verification.md",
        data["verification"].replace("状态：TODO", "状态：PASS"),
    )
    mark_intent_review_pass(store, requirement_id)
    store.touch_meta(requirement_id, task_provider="dashi")

    class FakeDashi:
        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            return (Task(id="TASK-001", title="Pending", status="in_progress"),)

    result = review_requirement(store, requirement_id, task_provider=FakeDashi())  # type: ignore[arg-type]

    assert result.passed is False
    assert result.blockers == ("任务尚未达到可审查状态：TASK-001 [in_progress]",)


def test_intent_review_reports_violation_when_any_check_violates(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Over-designed change")
    data = store.load(requirement_id)
    intent = (
        data["intent"]
        .replace("：PARTIAL", "：PASS")
        .replace("- 不必要的复杂度：PASS", "- 不必要的复杂度：VIOLATION")
    )
    store.write_text(data["path"] / "intent.md", intent)

    result = review_requirement(store, requirement_id)

    assert review_intent(intent).status is IntentStatus.VIOLATION
    assert result.intent_status is IntentStatus.VIOLATION
    assert any("意图一致性状态为 VIOLATION" in item for item in result.blockers)
