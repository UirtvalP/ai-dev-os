"""统一 Runtime Contract 的 Provider 无关行为。"""

from __future__ import annotations

from pathlib import Path

from workspace_orchestrator.agent_runtime import (
    AgentEvent,
    AgentRunRequest,
    AgentRuntime,
    ExecutionSpec,
    ModelDescriptor,
    RuntimeDescriptor,
    RuntimeEventStore,
    RuntimeOperationResult,
    RuntimeSessionRef,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []
        self.interrupts: list[tuple[RuntimeSessionRef, str]] = []
        self.closed = False
        self.state = "running"

    def describe(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            "fake", "Fake", "1", True,
            ("start", "resume", "read", "message", "interrupt", "archive", "events", "models"),
            (ModelDescriptor("m", "Model", ("low", "high")),),
        )

    def start(self, request: AgentRunRequest) -> RuntimeOperationResult:
        self.requests.append(request)
        session = RuntimeSessionRef(
            "fake", "session-1", request.run_id, str(request.workspace_path),
            execution_id=request.execution_id, sandbox=request.sandbox, model=request.model,
            reasoning_effort=request.reasoning_effort,
            requirement_id=request.requirement_id, task_id=request.task_id,
        )
        return RuntimeOperationResult("ok", session, "turn-1")

    def resume(self, request: AgentRunRequest) -> RuntimeOperationResult:
        self.requests.append(request)
        session = RuntimeSessionRef(
            "fake", str(request.resume_session_id), request.run_id, str(request.workspace_path),
            execution_id=request.execution_id, sandbox=request.sandbox, model=request.model,
            reasoning_effort=request.reasoning_effort,
            requirement_id=request.requirement_id, task_id=request.task_id,
        )
        return RuntimeOperationResult("ok", session, "turn-2")

    def read_session(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", session, data={"state": self.state})

    def send_message(self, session: RuntimeSessionRef, text: str) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", session, "turn-3", {"message": text})

    def steer(self, session, turn_id, text):
        return RuntimeOperationResult("unsupported", session)

    def interrupt(self, session: RuntimeSessionRef, turn_id: str) -> RuntimeOperationResult:
        self.interrupts.append((session, turn_id))
        return RuntimeOperationResult("ok", session, turn_id)

    def archive(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", session)

    def respond_to_request(self, session, request_id, decision):
        return RuntimeOperationResult("unsupported", session)

    def wait(self, session, turn_id, *, timeout_seconds):
        raise AssertionError("本契约测试不等待 Provider")

    def close(self) -> None:
        self.closed = True


def test_descriptor_exposes_canonical_capabilities_without_breaking_legacy_names() -> None:
    descriptor = FakeAdapter().describe()
    assert descriptor.supports("message")
    assert descriptor.supports("interactive_message")
    assert descriptor.supports("cancel")
    assert descriptor.supports("reasoning_selection")
    assert descriptor.canonical_capabilities == (
        "start", "resume", "interactive_message", "event_stream", "cancel",
        "status", "archive", "model_selection", "reasoning_selection", "tool_events",
    )
    canonical_only = RuntimeDescriptor(
        "future", "Future", "1", True, ("interactive_message", "event_stream"),
    )
    assert canonical_only.supports("message") and canonical_only.supports("events")


def test_standard_runtime_start_resume_status_cancel_and_events(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    event_store = RuntimeEventStore(tmp_path / "events")
    runtime = AgentRuntime(adapter, event_store)
    opened = runtime.start(ExecutionSpec(
        "EXE-1", tmp_path, "start", "EXE-1", sandbox="read-only", model="m",
        reasoning_effort="high", requirement_id="REQ-1",
        task_id="TASK-1",
    ))
    assert opened.ok and opened.session
    session = opened.session
    assert runtime.list_models()[0].id == "m"
    assert runtime.status(session).data == {"state": "running", "active_turn_id": "turn-1"}

    event_store.append(AgentEvent(
        "event-1", "EXE-1", "fake", "message", {"text": "visible"},
        session_id=session.session_id, execution_id="EXE-1",
    ))
    assert [event.event_id for event in runtime.stream_events(session)] == ["event-1"]
    assert runtime.cancel(session).ok
    assert adapter.interrupts == [(session, "turn-1")]

    resumed = runtime.resume(session, "continue")
    assert resumed.ok
    request = adapter.requests[-1]
    assert request.resume_session_id == "session-1"
    assert (request.sandbox, request.model, request.reasoning_effort) == ("read-only", "m", "high")
    assert request.execution_id == "EXE-1"
    assert (request.requirement_id, request.task_id) == ("REQ-1", "TASK-1")
    adapter.state = "completed"
    assert runtime.status(session).data["active_turn_id"] is None
    assert runtime.cancel(session).status == "unsupported"
    assert runtime.archive(session).ok
    runtime.close()
    assert adapter.closed


def test_resume_rejects_legacy_reference_without_execution_constraints(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    runtime = AgentRuntime(adapter, RuntimeEventStore(tmp_path / "events"))
    result = runtime.resume(RuntimeSessionRef("fake", "legacy-session"), "continue")
    assert result.status == "failed"
    assert result.error and result.error.code == "incomplete_session_ref"
    assert adapter.requests == []


def test_completion_event_clears_turn_when_provider_status_is_unsupported(tmp_path: Path) -> None:
    class NoStatusAdapter(FakeAdapter):
        def read_session(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
            return RuntimeOperationResult("unsupported", session)

    adapter = NoStatusAdapter()
    event_store = RuntimeEventStore(tmp_path / "events")
    runtime = AgentRuntime(adapter, event_store)
    opened = runtime.start(ExecutionSpec("EXE-2", tmp_path, "run", "EXE-2"))
    assert opened.session
    event_store.append(AgentEvent(
        "done", "EXE-2", "fake", "completion", {},
        session_id=opened.session.session_id, turn_id="turn-1", execution_id="EXE-2",
    ))
    assert runtime.status(opened.session).status == "unsupported"
    assert runtime.cancel(opened.session).status == "unsupported"
    assert adapter.interrupts == []
