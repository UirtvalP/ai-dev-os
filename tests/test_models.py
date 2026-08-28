from workspace_orchestrator.models import Requirement, WorkflowComplexity, Workspace


def test_workspace_defaults_to_normal_workflow() -> None:
    requirement = Requirement(id="REQ-001", title="Demo", goal="Prove restore works")

    workspace = Workspace(requirement=requirement)

    assert workspace.complexity is WorkflowComplexity.NORMAL
    assert workspace.session_ids == []
