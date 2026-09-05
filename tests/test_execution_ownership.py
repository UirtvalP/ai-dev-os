"""真实 V1 Dispatcher 与 V2 持久认领/投影的交叉回归；只假外部 Provider/Executor。"""

import multiprocessing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_dispatcher import FakeExecutor, FakeTasks, _store
from test_orchestration_projection import snapshot

from workspace_orchestrator.adapters.base import TaskProvider
from workspace_orchestrator.automation.dispatcher import AutoDispatcher
from workspace_orchestrator.execution_ownership import ExecutionOwnership, ExecutionOwnershipError
from workspace_orchestrator.models import Task
from workspace_orchestrator.orchestration.contracts import ExecutionPlan, PlanningRequest, TaskSpec
from workspace_orchestrator.orchestration.projection import TaskProjection
from workspace_orchestrator.orchestration.store import OrchestrationStore
from workspace_orchestrator.workspace import WorkspaceStore


class Provider(FakeTasks):
    def compare_and_set_status(self, task_id, *, expected_version, expected_status, status):
        assert (self.task.version, self.task.status) == (expected_version, expected_status)
        return self.update_status(task_id, status)


def setup(tmp_path, monkeypatch):
    workspace, requirement_id = _store(tmp_path)
    provider = Provider(Task("AID-1", "卡片", raw_id="opaque-1", status="todo", version=1,
                             labels=(f"requirement:{requirement_id}",)))
    executor = FakeExecutor(provider)
    monkeypatch.setattr("workspace_orchestrator.automation.dispatcher.configured_task_provider",
                        lambda *args: provider)
    ownership = ExecutionOwnership(workspace)
    task = TaskSpec("T1", "V2 节点", "候选", worktree=str(tmp_path))
    request = PlanningRequest(requirement_id, "目标", (task,),
                              extra={"task_provider_bindings": {"T1": "AID-1"}})
    plan = ExecutionPlan("plan", requirement_id, "direct", (task,))
    return workspace, provider, executor, ownership, request, plan


def test_v2_projection_cannot_cause_v1_duplicate_even_after_restart(tmp_path, monkeypatch):
    workspace, provider, executor, ownership, request, plan = setup(tmp_path, monkeypatch)
    ownership.claim_plan(request, plan, lambda: cast(TaskProvider, provider))
    projector = TaskProjection(OrchestrationStore(tmp_path / "projection"),
                               cast(TaskProvider, provider), {"T1": "AID-1"},
                               ownership_check=ownership.require_v2)
    state = snapshot(1, 1, "running")
    state["data"]["requirement_id"] = request.requirement_id
    assert projector.sync(state)["status"] == "synced"
    assert provider.task.status == "in_progress"
    assert AutoDispatcher(workspace, executor).run_once() == "idle"
    restored = ExecutionOwnership(workspace)
    def offline():
        pytest.fail("已持久认领不应重新读取离线 Provider")
    restored.claim_plan(request, plan, offline)
    with pytest.raises(ExecutionOwnershipError):
        restored.require_v1(replace(provider.task, id="opaque-1", raw_id=None))
    assert not executor.calls


def test_v1_claim_wins_before_v2_and_cannot_be_stolen(tmp_path, monkeypatch):
    workspace, provider, executor, ownership, request, plan = setup(tmp_path, monkeypatch)
    provider.update_status("AID-1", "in_progress")
    assert AutoDispatcher(workspace, executor)._claim(provider.task, request.requirement_id)
    with pytest.raises(ExecutionOwnershipError, match="旧 Dispatcher"):
        ownership.claim_plan(request, plan, lambda: cast(TaskProvider, provider))
    assert ownership.store.snapshot()["data"] == {}


def test_candidate_selected_before_v2_claim_rechecks_without_mutating_card(tmp_path, monkeypatch):
    workspace, provider, executor, ownership, request, plan = setup(tmp_path, monkeypatch)
    provider.update_status("AID-1", "in_progress")
    dispatcher = AutoDispatcher(workspace, executor)
    candidate = dispatcher._candidate()
    assert candidate is not None
    ownership.claim_plan(request, plan, lambda: cast(TaskProvider, provider))
    monkeypatch.setattr(AutoDispatcher, "_candidate", lambda self: candidate)
    before = provider.task
    assert dispatcher.run_once() == "blocked"
    assert not executor.calls and provider.task == before


def test_unclaimed_projection_never_exposes_executable_state(tmp_path, monkeypatch):
    _, provider, _, _, request, _ = setup(tmp_path, monkeypatch)
    projector = TaskProjection(OrchestrationStore(tmp_path / "projection"),
                               cast(TaskProvider, provider), {"T1": "AID-1"})
    state = snapshot(1, 1, "running")
    state["data"]["requirement_id"] = request.requirement_id
    result = projector.sync(state)
    assert result["entries"]["T1"]["last_error"]["code"] == "execution_unclaimed"
    assert provider.task.status == "todo"


def test_v2_binding_cannot_be_moved_or_bypass_an_active_session(tmp_path, monkeypatch):
    _, provider, _, ownership, request, plan = setup(tmp_path, monkeypatch)
    provider.task = replace(provider.task, binding_session_id="other-session")
    with pytest.raises(ExecutionOwnershipError):
        ownership.claim_plan(request, plan, lambda: cast(TaskProvider, provider))
    provider.task = replace(provider.task, binding_session_id=None)
    ownership.claim_plan(request, plan, lambda: cast(TaskProvider, provider))
    other = replace(request.tasks[0], task_id="other")
    changed = replace(request, tasks=(other,), extra={"task_provider_bindings": {"other": "AID-1"}})
    with pytest.raises(ExecutionOwnershipError):
        ownership.claim_plan(changed, replace(plan, nodes=(other,)), lambda: cast(TaskProvider, provider))


def _race_claim(root, requirement_id, engine, request, plan, start, results):
    workspace = WorkspaceStore(Path(root))
    provider = Provider(Task("AID-1", "卡片", raw_id="opaque-1", status="in_progress", version=1))
    assert start.wait(10)
    try:
        if engine == "v1":
            assert AutoDispatcher(workspace, FakeExecutor(provider))._claim(provider.task, requirement_id)
        else:
            ExecutionOwnership(workspace).claim_plan(
                PlanningRequest.from_dict(request), ExecutionPlan.from_dict(plan), lambda: provider,
            )
        results.put((engine, "claimed"))
    except ExecutionOwnershipError:
        results.put((engine, "blocked"))


def test_v1_v2_cross_process_claim_has_exactly_one_winner(tmp_path, monkeypatch):
    _, _, _, _, request, plan = setup(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    children = [context.Process(target=_race_claim, args=(
        str(tmp_path), request.requirement_id, engine, request.to_dict(), plan.to_dict(), start, results,
    )) for engine in ("v1", "v2")]
    try:
        for child in children:
            child.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in children]
        assert sorted(outcome for _, outcome in outcomes) == ["blocked", "claimed"]
        for child in children:
            child.join(10)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        results.close()


def test_corrupt_ownership_fails_closed_without_external_mutation(tmp_path, monkeypatch):
    workspace, provider, executor, ownership, request, plan = setup(tmp_path, monkeypatch)
    ownership.claim_plan(request, plan, lambda: cast(TaskProvider, provider))
    path = ownership.store.root / "state.json"
    path.write_text("{broken", encoding="utf-8")
    provider.update_status("AID-1", "in_progress")
    before = provider.task
    assert AutoDispatcher(workspace, executor).run_once() == "idle"
    assert provider.task == before and not executor.calls
    assert path.read_text(encoding="utf-8") == "{broken"
