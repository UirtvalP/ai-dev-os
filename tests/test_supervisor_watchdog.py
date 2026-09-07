from datetime import UTC, datetime
from pathlib import Path

import pytest

from workspace_orchestrator.agent_runtime.contracts import AgentEvent
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
from workspace_orchestrator.dashboard import CommandQueue, DashboardService
from workspace_orchestrator.executions import ExecutionStore
from workspace_orchestrator.main_agent import RequirementOwner
from workspace_orchestrator.supervisor_watchdog import RequirementWatchdog, WatchdogPolicy
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def stamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def test_watchdog_emits_deterministic_review_signals_without_killing_execution(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Watchdog", acceptance=["完成"])
    executions = ExecutionStore(store)
    events = RuntimeEventStore(store.root / "runtime-events")
    active = []
    for index in range(2):
        execution = executions.create(
            requirement_id, "TASK-SAME", role="worker", runtime_id="fake", prompt="run",
        )
        execution = executions.update(
            execution.id, status="running", session_id=f"session-{index}",
            started_at=stamp(100), last_progress_at=stamp(100),
        )
        active.append(execution)
    for index in range(3):
        events.append(AgentEvent(
            f"loop-{index}", active[0].id, "fake", "tool",
            {"command": "pytest", "usage": {"total_tokens": 250}},
            session_id=active[0].session_id, timestamp=stamp(100 + index),
            requirement_id=requirement_id, task_id="TASK-SAME",
            execution_id=active[0].id,
        ))
    for index in range(3):
        failed = executions.create(
            requirement_id, f"TASK-F-{index}", role="worker", runtime_id="fake", prompt="run",
        )
        executions.update(
            failed.id, status="failed", error={"code": "same", "message": "boom"},
        )
    policy = WatchdogPolicy(
        stuck_seconds=10, loop_threshold=3, repeated_failure_threshold=3,
        stagnation_seconds=50, stagnation_execution_count=2, token_budget=200,
    )
    watchdog = RequirementWatchdog(store, requirement_id, policy=policy, clock=lambda: 1000)
    first = watchdog.scan()
    watchdog.clock = lambda: 1100
    second = watchdog.scan()
    kinds = {item["kind"] for item in second["signals"]}
    assert {
        "ExecutionStuck", "ExecutionLooping", "RepeatedFailure", "BudgetWarning",
        "DuplicateWork", "NoRequirementProgress", "MainAgentReviewRequired",
    } <= kinds
    assert second["review_required"] is True and second["revision"] == 2
    assert all(executions.get(item.id).status == "running" for item in active)
    assert first["signals"][-1]["kind"] == "MainAgentReviewRequired"
    first_ids = {item["kind"]: item["id"] for item in first["signals"]}
    second_ids = {item["kind"]: item["id"] for item in second["signals"]}
    assert first_ids["ExecutionStuck"] == second_ids["ExecutionStuck"]


def test_watchdog_flows_into_main_agent_and_requirement_space(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Watchdog projection")
    execution = ExecutionStore(store).create(
        requirement_id, "TASK", role="worker", runtime_id="fake", prompt="run",
    )
    ExecutionStore(store).update(
        execution.id, status="running", started_at=stamp(1), last_progress_at=stamp(1),
    )
    RequirementWatchdog(
        store, requirement_id, policy=WatchdogPolicy(stuck_seconds=1), clock=lambda: 100,
    ).scan()
    owner = RequirementOwner(store, requirement_id).observe(expected_revision=0)
    assert owner.review_required is True
    assert any(item.startswith("watchdog=ExecutionStuck:") for item in owner.supervisor_signals)
    projection = DashboardService(
        store, RuntimeEventStore(store.root / "runtime-events"),
        CommandQueue(store.path_for(requirement_id) / "dashboard" / "commands.json"),
    ).requirement(requirement_id)["projection"]
    assert projection["supervisor_watchdog"]["review_required"] is True


def test_watchdog_rejects_event_with_cross_execution_identity(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Watchdog identity")
    execution = ExecutionStore(store).create(
        requirement_id, "TASK", role="worker", runtime_id="fake", prompt="run",
    )
    execution = ExecutionStore(store).update(
        execution.id, status="running", session_id="session", started_at=stamp(1),
    )
    RuntimeEventStore(store.root / "runtime-events").append(AgentEvent(
        "foreign", execution.id, "fake", "message", {}, session_id="other",
        requirement_id=requirement_id, task_id="TASK", execution_id=execution.id,
    ))
    with pytest.raises(WorkspaceError, match="身份与 Execution 不一致"):
        RequirementWatchdog(store, requirement_id, clock=lambda: 100).scan()


def test_watchdog_requires_none_session_events_for_unbound_execution(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Watchdog unbound identity")
    execution = ExecutionStore(store).create(
        requirement_id, "TASK", role="worker", runtime_id="fake", prompt="run",
    )
    ExecutionStore(store).update(execution.id, status="running", started_at=stamp(1))
    RuntimeEventStore(store.root / "runtime-events").append(AgentEvent(
        "foreign-unbound", execution.id, "fake", "message", {}, session_id="foreign",
        requirement_id=requirement_id, task_id="TASK", execution_id=execution.id,
    ))
    with pytest.raises(WorkspaceError, match="身份与 Execution 不一致"):
        RequirementWatchdog(store, requirement_id, clock=lambda: 100).scan()


def test_watchdog_rejects_forged_review_state(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Watchdog forged")
    path = store.path_for(requirement_id) / "supervisor-watchdog.json"
    store.write_json(path, {
        "schema_version": 1, "requirement_id": requirement_id, "revision": 1,
        "review_required": "false", "signals": [], "observation": {},
    })
    with pytest.raises(WorkspaceError, match="状态身份、版本或结构不合法"):
        RequirementOwner(store, requirement_id).observe(expected_revision=0)


def test_requirement_progress_fingerprint_resets_stagnation_clock(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Watchdog progress", acceptance=["done"])
    executions = ExecutionStore(store)
    for index in range(2):
        executions.create(
            requirement_id, f"TASK-{index}", role="worker", runtime_id="fake", prompt="run",
        )
    policy = WatchdogPolicy(stagnation_seconds=50, stagnation_execution_count=2)
    watchdog = RequirementWatchdog(store, requirement_id, policy=policy, clock=lambda: 100)
    watchdog.scan()
    verification = store.path_for(requirement_id) / "verification.md"
    store.write_text(verification, "# 验证\n\n## 最新检查\n\n新测试进展\n")
    watchdog.clock = lambda: 200
    state = watchdog.scan()
    assert not any(item["kind"] == "NoRequirementProgress" for item in state["signals"])
    assert state["observation"]["progress_changed_at"] == 200
