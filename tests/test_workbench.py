"""Workbench 主动启动 Execution 的纵向路径。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from workspace_orchestrator.agent_runtime import (
    AgentEvent,
    ExecutionSpec,
    RuntimeDescriptor,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
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


class ReplyRuntime(RecordingRuntime):
    def __init__(self, events, *, supported: bool = True) -> None:
        super().__init__()
        self.events = events
        self.supported = supported
        self.resumed: list[tuple[RuntimeSessionRef, str]] = []
        self.sent: list[tuple[RuntimeSessionRef, str]] = []

    def describe(self):
        capabilities = ("resume", "interactive_message", "event_stream") if self.supported else (
            "start", "event_stream",
        )
        return RuntimeDescriptor("fake", "Fake", "1", True, capabilities)

    def start(self, spec):
        raise AssertionError("回复原 Session 不得调用 start")

    def resume(self, session, message):
        self.resumed.append((session, message))
        self.events.append(AgentEvent(
            "provider-reply", session.run_id, session.runtime_id, "message",
            {"provider_raw": {"text": message}}, session_id=session.session_id,
            turn_id="turn-reply", requirement_id=session.requirement_id,
            task_id=session.task_id, execution_id=session.execution_id,
        ))
        return RuntimeOperationResult("ok", session, "turn-reply")

    def send_message(self, session, message):
        self.sent.append((session, message))
        return RuntimeOperationResult("ok", session, "turn-next")


def _started_execution(store: WorkspaceStore):
    requirement_id = store.create("P7 reply")
    starter = RecordingRuntime()
    service = WorkbenchExecutionService(store, lambda _name, _events: starter)
    execution = service.start(WorkbenchStart(
        requirement_id, "TASK-P7", "start", "fake", creation_key="p7-start",
    ))
    service.close()
    return requirement_id, execution


def test_reply_resumes_original_session_and_persists_provider_raw_event(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)
    made: list[ReplyRuntime] = []

    def factory(_name, events):
        made.append(ReplyRuntime(events))
        return made[-1]

    service = WorkbenchExecutionService(store, factory)
    first = service.reply(
        requirement_id, execution.id, "继续原会话", command_id="reply-1",
    )
    second = service.reply(
        requirement_id, execution.id, "再发一条", command_id="reply-2",
    )
    assert first.ok and first.session and first.session.session_id == "session-1"
    assert first.data["delivery"] == "resume"
    assert second.ok and second.data["delivery"] == "send_message"
    assert [message for _, message in made[0].resumed] == ["继续原会话"]
    assert [message for _, message in made[0].sent] == ["再发一条"]
    event = service.events.replay(execution.id)[0]
    assert event.payload == {"provider_raw": {"text": "继续原会话"}}
    assert service.executions.get(execution.id).turn_id == "turn-next"
    service.close()


def test_reply_fails_closed_on_session_identity_mismatch(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)
    damaged = dict(execution.result)
    damaged["session_ref"] = {**damaged["session_ref"], "session_id": "foreign"}
    WorkbenchExecutionService(store, lambda *_: None).executions.update(
        execution.id, status="waiting", result=damaged,
    )
    with pytest.raises(WorkspaceError, match="身份与持久化 Execution 不一致"):
        WorkbenchExecutionService(store, lambda *_: None).reply(
            requirement_id, execution.id, "不能发送", command_id="mismatch",
        )


def test_reply_reports_unsupported_without_starting_new_agent(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)
    runtime = ReplyRuntime(RuntimeEventStore(store.root / "runtime-events"), supported=False)
    service = WorkbenchExecutionService(store, lambda *_: runtime)
    capability = service.reply_capability(requirement_id, execution.id)
    result = service.reply(
        requirement_id, execution.id, "继续", command_id="unsupported",
    )
    assert capability["supported"] is False
    assert result.status == "unsupported"
    assert result.data["continued_original_session"] is False
    assert "不会新建 Agent" in str(result.data["alternative"])
    assert runtime.resumed == [] and runtime.sent == [] and runtime.specs == []


def test_reply_durable_claim_prevents_concurrent_duplicate_resume(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)
    entered, release = Event(), Event()
    runtimes: list[ReplyRuntime] = []

    class BlockingReplyRuntime(ReplyRuntime):
        def resume(self, session, message):
            self.resumed.append((session, message))
            entered.set()
            assert release.wait(3)
            return RuntimeOperationResult("ok", session, "turn-once")

    def factory(_name, events):
        runtime = BlockingReplyRuntime(events)
        runtimes.append(runtime)
        return runtime

    first = WorkbenchExecutionService(store, factory)
    second = WorkbenchExecutionService(store, factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            first.reply, requirement_id, execution.id, "once", command_id="same-command",
        )
        assert entered.wait(3)
        duplicate = second.reply(
            requirement_id, execution.id, "once", command_id="same-command",
        )
        release.set()
        delivered = pending.result(timeout=3)
    assert delivered.ok
    assert duplicate.error and duplicate.error.code == "reply_in_progress"
    assert len(runtimes) == 1 and len(runtimes[0].resumed) == 1
    replay = second.reply(
        requirement_id, execution.id, "once", command_id="same-command",
    )
    assert replay.ok and replay.data["idempotent_replay"] is True
    first.close()


def test_reply_rejects_foreign_session_even_for_failed_provider_result(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)

    class ForeignFailure(ReplyRuntime):
        def resume(self, session, message):
            foreign = RuntimeSessionRef("fake", "foreign", session.run_id, session.workspace_path)
            return RuntimeOperationResult("unsupported", foreign)

    service = WorkbenchExecutionService(
        store, lambda _name, events: ForeignFailure(events),
    )
    result = service.reply(
        requirement_id, execution.id, "continue", command_id="foreign-result",
    )
    assert result.error and result.error.code == "identity_mismatch"
    assert result.session and result.session.session_id == "session-1"


def test_reply_cleanup_failure_retains_runtime_for_retry(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)

    class RetryCleanup(ReplyRuntime):
        def __init__(self, events):
            super().__init__(events, supported=False)
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("retry cleanup")

    runtime = RetryCleanup(RuntimeEventStore(store.root / "runtime-events"))
    service = WorkbenchExecutionService(store, lambda *_: runtime)
    result = service.reply(
        requirement_id, execution.id, "continue", command_id="cleanup-reply",
    )
    assert result.status == "unsupported" and service._pending_cleanup == [runtime]
    service.close()
    assert runtime.close_calls == 2 and service._pending_cleanup == []


def test_reply_persist_failure_is_durable_unknown_and_never_resends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)
    runtime = ReplyRuntime(RuntimeEventStore(store.root / "runtime-events"))
    service = WorkbenchExecutionService(store, lambda *_: runtime)
    original_update = service.executions.update

    def fail_update(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service.executions, "update", fail_update)
    first = service.reply(
        requirement_id, execution.id, "once", command_id="persist-failure",
    )
    monkeypatch.setattr(service.executions, "update", original_update)
    second = service.reply(
        requirement_id, execution.id, "once", command_id="persist-failure",
    )
    assert first.error and first.error.code == "execution_persist_failed"
    assert first.data["delivery_accepted"] is True
    assert second.error and second.error.code == "execution_persist_failed"
    assert second.data["idempotent_replay"] is True
    assert len(runtime.resumed) == 1 and runtime.sent == []
    service.close()


def test_reply_receipt_replay_revalidates_session_identity(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)
    runtime = ReplyRuntime(RuntimeEventStore(store.root / "runtime-events"))
    service = WorkbenchExecutionService(store, lambda *_: runtime)
    assert service.reply(
        requirement_id, execution.id, "once", command_id="tamper-receipt",
    ).ok
    path = (
        store.path_for(requirement_id) / "executions" / "replies" / "tamper-receipt.json"
    )
    receipt = store.read_json(path)
    receipt["operation"]["session"]["session_id"] = "foreign"
    store.write_json(path, receipt)
    with pytest.raises(WorkspaceError, match="receipt 的 Session 身份不匹配"):
        service.reply(
            requirement_id, execution.id, "once", command_id="tamper-receipt",
        )
    service.close()


def test_close_waits_for_inflight_reply_and_closes_registered_runtime(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id, execution = _started_execution(store)
    entered, release = Event(), Event()

    class BlockingReplyRuntime(ReplyRuntime):
        def resume(self, session, message):
            entered.set()
            assert release.wait(3)
            return RuntimeOperationResult("ok", session, "turn-close")

    runtime = BlockingReplyRuntime(RuntimeEventStore(store.root / "runtime-events"))
    service = WorkbenchExecutionService(store, lambda *_: runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replying = pool.submit(
            service.reply, requirement_id, execution.id, "once", command_id="close-race",
        )
        assert entered.wait(3)
        closing = pool.submit(service.close)
        assert not closing.done()
        release.set()
        assert replying.result(timeout=3).ok
        closing.result(timeout=3)
    assert runtime.closed and service._runtimes == {}
