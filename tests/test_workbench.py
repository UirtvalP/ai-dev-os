"""Workbench 主动启动 Execution 的纵向路径。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from workspace_orchestrator.agent_runtime import (
    ExecutionSpec,
    RuntimeDescriptor,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from workspace_orchestrator.workbench import WorkbenchExecutionService, WorkbenchStart
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


class RecordingRuntime:
    def __init__(self) -> None:
        self.specs: list[ExecutionSpec] = []
        self.closed = False

    def describe(self):
        return RuntimeDescriptor("fake", "Fake", "1", True, ("start",))

    def list_models(self):
        return ()

    def start(self, spec: ExecutionSpec):
        self.specs.append(spec)
        return RuntimeOperationResult("ok", RuntimeSessionRef(
            "fake", "session-1", spec.run_id, str(spec.workspace_path),
            execution_id=spec.execution_id, sandbox=spec.sandbox,
            model=spec.model, reasoning_effort=spec.reasoning_effort,
            requirement_id=spec.requirement_id, task_id=spec.task_id,
        ), "turn-1")

    def resume(self, session, message):
        raise AssertionError

    def send_message(self, session, message):
        raise AssertionError

    def cancel(self, session):
        raise AssertionError

    def status(self, session):
        raise AssertionError

    def list_events(self, session, *, after=0, limit=1000):
        return ()

    def stream_events(self, session, *, after=0, limit=1000):
        return ()

    def archive(self, session):
        raise AssertionError

    def close(self):
        self.closed = True


def test_workbench_creates_execution_before_runtime_start_and_preserves_identity(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Workbench")
    runtime = RecordingRuntime()
    service = WorkbenchExecutionService(store, lambda _name, _events: runtime)
    execution = service.start(WorkbenchStart(
        requirement_id, "TASK-1", "implement", "fake", creation_key="request-1",
    ))
    assert execution.status == "running"
    assert execution.session_id == "session-1" and execution.turn_id == "turn-1"
    assert runtime.specs[0].execution_id == execution.id
    assert runtime.specs[0].requirement_id == requirement_id
    assert execution.result["session_ref"]["sandbox"] == "workspace-write"
    assert service.start(WorkbenchStart(
        requirement_id, "TASK-1", "implement", "fake", creation_key="request-1",
    )).id == execution.id
    assert len(runtime.specs) == 1
    service.close()
    assert runtime.closed


def test_workbench_creation_key_rejects_different_request(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Workbench")
    runtime = RecordingRuntime()
    service = WorkbenchExecutionService(store, lambda _name, _events: runtime)
    service.start(WorkbenchStart(
        requirement_id, "TASK-1", "first", "fake", creation_key="same-key",
    ))
    with pytest.raises(WorkspaceError, match="已绑定不同启动请求"):
        service.start(WorkbenchStart(
            requirement_id, "TASK-2", "different", "fake", creation_key="same-key",
        ))
    assert len(runtime.specs) == 1
    service.close()


def test_workbench_persists_failed_execution_when_runtime_factory_raises(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Workbench")

    def fail(_name, _events):
        raise OSError("runtime missing")

    execution = WorkbenchExecutionService(store, fail).start(WorkbenchStart(
        requirement_id, "TASK-FAIL", "implement", "missing",
    ))
    assert execution.status == "failed"
    assert execution.error and execution.error["code"] == "runtime_start_failed"
    assert execution.session_id is None


def test_workbench_records_cleanup_unknown_instead_of_leaving_starting(tmp_path: Path) -> None:
    class BadIdentityRuntime(RecordingRuntime):
        def start(self, spec: ExecutionSpec):
            return RuntimeOperationResult("ok", RuntimeSessionRef(
                "wrong", "session", spec.run_id, str(spec.workspace_path),
                execution_id=spec.execution_id, sandbox=spec.sandbox,
                requirement_id=spec.requirement_id, task_id=spec.task_id,
            ), "turn")

        def close(self):
            raise OSError("cleanup uncertain")

    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Workbench")
    execution = WorkbenchExecutionService(
        store, lambda _name, _events: BadIdentityRuntime(),
    ).start(WorkbenchStart(requirement_id, "TASK", "run", "fake"))
    assert execution.status == "failed"
    assert execution.error and execution.error["code"] == "runtime_cleanup_unknown"


def test_same_creation_key_is_started_once_while_first_claim_is_running(tmp_path: Path) -> None:
    entered = Event()
    release = Event()

    class BlockingRuntime(RecordingRuntime):
        def start(self, spec: ExecutionSpec):
            entered.set()
            assert release.wait(3)
            return super().start(spec)

    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Workbench")
    runtimes: list[BlockingRuntime] = []

    def factory(_name, _events):
        runtime = BlockingRuntime()
        runtimes.append(runtime)
        return runtime

    first_service = WorkbenchExecutionService(store, factory)
    second_service = WorkbenchExecutionService(store, factory)
    request = WorkbenchStart(
        requirement_id, "TASK", "run", "fake", creation_key="concurrent",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_service.start, request)
        assert entered.wait(3)
        observed = pool.submit(second_service.start, request).result(timeout=3)
        assert observed.status == "starting"
        release.set()
        completed = first.result(timeout=3)
    assert completed.status == "running"
    assert len(runtimes) == 1 and len(runtimes[0].specs) == 1
    first_service.close()
    assert first_service.executions.get(completed.id).status == "waiting"


def test_close_keeps_failed_runtime_reference_and_continues_cleanup(tmp_path: Path) -> None:
    class RetryCloseRuntime(RecordingRuntime):
        def __init__(self, fail_once: bool) -> None:
            super().__init__()
            self.fail_once = fail_once
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            if self.fail_once and self.close_calls == 1:
                raise OSError("retry me")
            self.closed = True

    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Workbench")
    runtimes = [RetryCloseRuntime(True), RetryCloseRuntime(False)]
    service = WorkbenchExecutionService(store, lambda _name, _events: runtimes.pop(0))
    first = service.start(WorkbenchStart(requirement_id, "T1", "one", "fake"))
    second = service.start(WorkbenchStart(requirement_id, "T2", "two", "fake"))
    first_runtime = service._runtimes[first.id]
    second_runtime = service._runtimes[second.id]
    with pytest.raises(WorkspaceError, match="清理未确认"):
        service.close()
    assert not first_runtime.closed and second_runtime.closed
    assert first.id in service._runtimes and second.id not in service._runtimes
    service.close()
    assert first_runtime.closed and service._runtimes == {}
