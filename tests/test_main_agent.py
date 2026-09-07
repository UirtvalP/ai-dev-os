"""Requirement Owner 状态与确定性决策循环。"""

from pathlib import Path

import pytest

from workspace_orchestrator.executions import ExecutionStore
from workspace_orchestrator.main_agent import RequirementOwner
from workspace_orchestrator.orchestration.store import OrchestrationStore
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def test_owner_observes_structured_workspace_without_provider_or_conversation(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create(
        "Owner demo", goal="完成独立工作台", acceptance=["状态可恢复", "动作结构化"],
    )
    executions = ExecutionStore(store)
    done = executions.create(
        requirement_id, "TASK-DONE", role="implementation", runtime_id="fake", prompt="done",
    )
    executions.update(done.id, status="completed")
    active = executions.create(
        requirement_id, "TASK-ACTIVE", role="implementation", runtime_id="fake", prompt="active",
    )
    executions.update(active.id, status="running")

    owner = RequirementOwner(store, requirement_id)
    state = owner.observe(expected_revision=0)
    assert state.current_goal == "完成独立工作台"
    assert state.active_tasks == ("TASK-ACTIVE",)
    assert state.active_executions == (active.id,)
    assert state.completed_tasks == ("TASK-DONE",)
    assert state.acceptance_status.total == 2
    assert state.acceptance_criteria == ("状态可恢复", "动作结构化")
    assert isinstance(state.intent, dict) and isinstance(state.git_state, dict)
    assert state.actions == () and state.loop_stage == "observe"
    assert owner.path.is_file()
    assert "runtime_id" not in state.to_dict()


def test_owner_loop_uses_cas_stage_and_records_structured_action(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Owner demo", goal="ship")
    owner = RequirementOwner(store, requirement_id)
    state = owner.observe(expected_revision=0)
    state = owner.advance("observe", expected_revision=state.revision)
    state = owner.advance("assess", expected_revision=state.revision)
    planned = owner.advance("plan", expected_revision=state.revision)
    assert planned.loop_stage == "act"
    acted = owner.record_action(
        "CreateTask", "拆分下一步", {"title": "P4 demo"},
        expected_revision=planned.revision,
    )
    assert acted.loop_stage == "inspect"
    assert acted.actions[-1].kind == "CreateTask"
    assert acted.actions[-1].payload == {"title": "P4 demo"}
    refreshed = owner.observe(expected_revision=acted.revision)
    assert refreshed.loop_stage == "inspect"
    with pytest.raises(WorkspaceError, match="revision 已变化"):
        owner.advance("inspect", expected_revision=acted.revision)
    with pytest.raises(WorkspaceError, match="只有 Main Agent act"):
        owner.record_action("AskUser", "重复", {}, expected_revision=refreshed.revision)


def test_owner_rejects_non_json_action_payload(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Owner demo", goal="ship")
    owner = RequirementOwner(store, requirement_id)
    state = owner.observe(expected_revision=0)
    state = owner.advance("observe", expected_revision=state.revision)
    state = owner.advance("assess", expected_revision=state.revision)
    state = owner.advance("plan", expected_revision=state.revision)
    with pytest.raises(WorkspaceError, match="Action 不合法"):
        owner.record_action(
            "CreateTask", "invalid", {"value": object()}, expected_revision=state.revision,
        )
    assert owner.load().loop_stage == "act"


def test_owner_restores_done_and_supervisor_facts(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Owner demo", goal="ship", acceptance=["done"])
    meta_path = store.path_for(requirement_id) / "meta.json"
    meta = store.read_json(meta_path)
    meta["status"] = "done"
    store.write_json(meta_path, meta)
    ledger = OrchestrationStore(
        store.path_for(requirement_id) / "orchestration" / "supervisor",
    )
    lease = ledger.acquire("test")
    ledger.mutate(lease, lambda data: data.update(nodes={"TASK-1": {"state": "running"}}))
    ledger.release(lease)

    state = RequirementOwner(store, requirement_id).observe(expected_revision=0)
    assert state.acceptance_status.status == "passed"
    assert any(item.startswith("revision=") for item in state.supervisor_signals)
    assert "node=TASK-1:running" in state.supervisor_signals
