from pathlib import Path

import pytest

from workspace_orchestrator.models import Task, WorkflowComplexity
from workspace_orchestrator.review import review_requirement
from workspace_orchestrator.workflow import route_workflow, upgrade_workflow
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("Fix a typo in the login copy", WorkflowComplexity.TINY),
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
    with pytest.raises(WorkspaceError, match="only upgrade"):
        upgrade_workflow(store, requirement_id, WorkflowComplexity.TINY, "Try to downgrade")


def test_review_blocks_until_acceptance_and_verification_pass(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo", acceptance=["Feature works"])

    blocked = review_requirement(store, requirement_id)

    assert blocked.passed is False
    assert store.load(requirement_id)["meta"]["status"] == "draft"
    assert any("Acceptance criterion" in item for item in blocked.blockers)
    assert any("Verification" in item for item in blocked.blockers)


def test_review_enters_in_review_but_never_done(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo", acceptance=["Feature works"])
    data = store.load(requirement_id)
    requirement = data["requirement"].replace("- [ ] Feature works", "- [x] Feature works")
    verification = data["verification"].replace("Status: TODO", "Status: PASS")
    store.write_text(data["path"] / "requirement.md", requirement)
    store.write_text(data["path"] / "verification.md", verification)

    result = review_requirement(store, requirement_id)

    assert result.passed is True
    assert store.load(requirement_id)["meta"]["status"] == "in_review"


def test_review_checks_configured_task_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo", acceptance=["Feature works"])
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "requirement.md",
        data["requirement"].replace("- [ ] Feature works", "- [x] Feature works"),
    )
    store.write_text(
        data["path"] / "verification.md",
        data["verification"].replace("Status: TODO", "Status: PASS"),
    )
    store.touch_meta(requirement_id, task_provider="dashi")

    class FakeDashi:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            return (Task(id="TASK-001", title="Pending", status="in_progress"),)

    monkeypatch.setattr("workspace_orchestrator.review.DashiTaskProvider", FakeDashi)

    result = review_requirement(store, requirement_id)

    assert result.passed is False
    assert result.blockers == ("Task is not review-ready: TASK-001 [in_progress]",)
