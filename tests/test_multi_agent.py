from pathlib import Path

import pytest

from workspace_orchestrator.agent_runtime.contracts import AgentEvent
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
from workspace_orchestrator.executions import ExecutionStore
from workspace_orchestrator.integration.contracts import MergeReceipt
from workspace_orchestrator.multi_agent import (
    ExecutionTrackedWorkerPort,
    IntegrationExecutionService,
)
from workspace_orchestrator.orchestration.contracts import (
    ModelRoute,
    TaskSpec,
    WorkerIsolation,
    WorkerObservation,
)
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


class FakeWorker:
    def __init__(self) -> None:
        self.observations: dict[str, WorkerObservation] = {}
        self.dispatches = 0

    def isolation(self, task: TaskSpec) -> WorkerIsolation:
        return WorkerIsolation("fake", True, (str(task.worktree),), ())

    def dispatch(self, attempt_id, fence, task, route):
        self.dispatches += 1
        result = WorkerObservation(attempt_id, fence, "running", session_id="session-1")
        self.observations[attempt_id] = result
        return result

    def poll(self, attempt_id, fence):
        return self.observations[attempt_id]

    reconcile = poll

    def cancel(self, attempt_id, fence):
        result = WorkerObservation(
            attempt_id, fence, "failed", session_id="session-1", error_class="cancelled",
        )
        self.observations[attempt_id] = result
        return result


def test_worker_attempt_is_first_class_execution_with_raw_events(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Multi Agent")
    source = RuntimeEventStore(tmp_path / "worker-events")
    worker = FakeWorker()
    tracked = ExecutionTrackedWorkerPort(
        store, requirement_id, worker, source_events=source,
    )
    worktree = tmp_path / "task-a"
    worktree.mkdir()
    task = TaskSpec(
        "TASK-A", "A", "implement", write_required=True,
        worktree=str(worktree), branch="feat/a",
    )
    route = ModelRoute("fake", "model", None, "workspace-write")
    running = tracked.dispatch("attempt-a", 1, task, route)
    execution = ExecutionStore(store).list(requirement_id)[0]
    assert running.state == "running" and execution.status == "running"
    assert execution.task_id == "TASK-A" and execution.worktree == str(worktree)
    assert execution.execution_policy["attempt_id"] == "attempt-a"
    assert execution.result["session_ref"]["execution_id"] == execution.id
    assert execution.result["session_ref"]["run_id"] == execution.id
    source.append(AgentEvent(
        "raw-1", "attempt-a", "fake", "tool", {"provider_raw": {"command": "test"}},
        session_id="session-1", requirement_id=requirement_id, task_id="TASK-A",
    ))
    worker.observations["attempt-a"] = WorkerObservation(
        "attempt-a", 1, "candidate_complete", session_id="session-1",
        candidate_sha="a" * 40, candidate_tree="b" * 40,
    )
    tracked.poll("attempt-a", 1)
    completed = ExecutionStore(store).get(execution.id)
    assert completed.status == "completed" and completed.event_cursor == 1
    event = RuntimeEventStore(store.root / "runtime-events").replay(execution.id)[0]
    assert event.execution_id == execution.id
    assert event.payload == {"provider_raw": {"command": "test"}}


def test_tracked_dispatch_is_idempotent_for_same_attempt(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Idempotent worker")
    worker = FakeWorker()
    tracked = ExecutionTrackedWorkerPort(store, requirement_id, worker)
    worktree = tmp_path / "task"
    worktree.mkdir()
    task = TaskSpec("TASK", "Task", "run", worktree=str(worktree))
    route = ModelRoute("fake", "model", None, "read-only")
    tracked.dispatch("same-attempt", 2, task, route)
    tracked.dispatch("same-attempt", 2, task, route)
    assert worker.dispatches == 2  # delegate owns idempotency; only one Execution is created.
    assert len(ExecutionStore(store).list(requirement_id)) == 1


def test_tracked_attempt_rejects_changed_prompt_and_session(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Stable identity")
    worker = FakeWorker()
    tracked = ExecutionTrackedWorkerPort(store, requirement_id, worker)
    worktree = tmp_path / "task"
    worktree.mkdir()
    task = TaskSpec("TASK", "Task", "run", worktree=str(worktree))
    route = ModelRoute("fake", "model", None, "read-only")
    tracked.dispatch("attempt", 1, task, route)
    with pytest.raises(WorkspaceError, match="不同 Execution 身份"):
        tracked.dispatch(
            "attempt", 1,
            TaskSpec("TASK", "Task", "changed", worktree=str(worktree)), route,
        )
    worker.observations["attempt"] = WorkerObservation(
        "attempt", 1, "running", session_id="session-2",
    )
    with pytest.raises(WorkspaceError, match="Session"):
        tracked.poll("attempt", 1)


def test_tracked_observe_error_marks_execution_waiting(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Ambiguous observation")
    worker = FakeWorker()
    tracked = ExecutionTrackedWorkerPort(store, requirement_id, worker)
    worktree = tmp_path / "task"
    worktree.mkdir()
    tracked.dispatch(
        "attempt", 1, TaskSpec("TASK", "Task", "run", worktree=str(worktree)),
        ModelRoute("fake", "model", None, "read-only"),
    )

    def broken(*_args):
        raise RuntimeError("transport lost")

    worker.poll = broken
    with pytest.raises(RuntimeError, match="transport lost"):
        tracked.poll("attempt", 1)
    execution = ExecutionStore(store).list(requirement_id)[0]
    assert execution.status == "waiting"
    assert execution.error == {"code": "ambiguous_poll", "message": "transport lost"}


def test_tracked_rejects_sessionless_event_once_session_is_known(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Exact event identity")
    source = RuntimeEventStore(tmp_path / "worker-events")
    worker = FakeWorker()
    tracked = ExecutionTrackedWorkerPort(store, requirement_id, worker, source_events=source)
    worktree = tmp_path / "task"
    worktree.mkdir()
    task = TaskSpec("TASK", "Task", "run", worktree=str(worktree))
    route = ModelRoute("fake", "model", None, "read-only")
    tracked.dispatch("attempt", 1, task, route)
    source.append(AgentEvent(
        "raw", "attempt", "fake", "tool", {"raw": True},
        requirement_id=requirement_id, task_id="TASK",
    ))
    with pytest.raises(WorkspaceError, match="原始事件身份"):
        tracked.poll("attempt", 1)
    execution = ExecutionStore(store).list(requirement_id)[0]
    assert execution.status == "waiting"
    assert execution.error and execution.error["code"] == "ambiguous_poll"


def test_tracked_recovery_cross_checks_persisted_execution_identity(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Tampered identity")
    worker = FakeWorker()
    tracked = ExecutionTrackedWorkerPort(store, requirement_id, worker)
    worktree = tmp_path / "task"
    worktree.mkdir()
    task = TaskSpec("TASK", "Task", "run", worktree=str(worktree))
    route = ModelRoute("fake", "model", None, "read-only")
    tracked.dispatch("attempt", 1, task, route)
    execution = ExecutionStore(store).list(requirement_id)[0]
    path = store.path_for(requirement_id) / "executions" / f"{execution.id}.json"
    payload = store.read_json(path)
    payload["execution_policy"]["route"]["runtime_id"] = "other"
    store.write_json(path, payload)
    with pytest.raises(WorkspaceError, match="恢复身份"):
        tracked.poll("attempt", 1)


def test_integration_execution_only_records_existing_gate_result(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Integration Execution")
    snapshot = {"data": {"nodes": {
        "A": {"status": "accepted"}, "B": {"status": "accepted"},
    }}}
    calls = 0

    def integrate():
        nonlocal calls
        calls += 1
        return MergeReceipt(
            "receipt-1", requirement_id, "merge-1", "merged",
            "a" * 40, "b" * 40, "c" * 40, "refs/ai-dev-os/integration/merge-1",
            "auth-1", "verify-1", "post-verify-1", "2026-09-07T00:00:00+00:00",
        ).to_dict()

    service = IntegrationExecutionService(store)
    first = service.run(
        requirement_id, ("A", "B"), command_id="merge-1",
        supervisor_snapshot=snapshot, integrate=integrate,
    )
    second = service.run(
        requirement_id, ("A", "B"), command_id="merge-1",
        supervisor_snapshot=snapshot, integrate=integrate,
    )
    assert first.id == second.id and first.status == "completed" and calls == 1
    assert first.role == "integration" and first.result["receipt"]["status"] == "merged"


def test_integration_execution_retries_unknown_gate_result_and_backfills(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Recover Integration Execution")
    snapshot = {"data": {"nodes": {"A": {"status": "accepted"}}}}
    calls = 0
    receipt = MergeReceipt(
        "receipt-2", requirement_id, "merge-2", "merged",
        "a" * 40, "b" * 40, "c" * 40, "refs/ai-dev-os/integration/merge-2",
        "auth-2", "verify-2", "post-2", "2026-09-07T00:00:00+00:00",
    ).to_dict()

    def integrate():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("lost reply")
        return receipt

    service = IntegrationExecutionService(store)
    blocked = service.run(
        requirement_id, ("A",), command_id="merge-2",
        supervisor_snapshot=snapshot, integrate=integrate,
    )
    completed = service.run(
        requirement_id, ("A",), command_id="merge-2",
        supervisor_snapshot=snapshot, integrate=integrate,
    )
    assert blocked.id == completed.id and blocked.status == "blocked"
    assert completed.status == "completed" and calls == 2


def test_integration_record_backfills_existing_unknown_execution(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Reconcile Integration Execution")
    snapshot = {"data": {"nodes": {}}}
    service = IntegrationExecutionService(store)
    service.run(
        requirement_id, (), command_id="merge-3", supervisor_snapshot=snapshot,
        integrate=lambda: (_ for _ in ()).throw(RuntimeError("unknown")),
    )
    receipt = MergeReceipt(
        "receipt-3", requirement_id, "merge-3", "merged",
        "a" * 40, "b" * 40, "c" * 40, "refs/ai-dev-os/integration/merge-3",
        "auth-3", "verify-3", "post-3", "2026-09-07T00:00:00+00:00",
    ).to_dict()
    execution = service.record(
        requirement_id, (), command_id="merge-3",
        supervisor_snapshot=snapshot, receipt=receipt,
    )
    assert execution.status == "completed" and execution.result["receipt"] == receipt
