"""Provider 无关执行桥的正向、负向、回退和资源清理契约。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from workspace_orchestrator.agent_runtime.contracts import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    RuntimeFailure,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
from workspace_orchestrator.agent_runtime.execution import RuntimeExecutor


class FakeRuntime:
    def __init__(self) -> None:
        self.requests = []
        self.closed = False
        self.resume_error = None
        self.start_error = None
        self.wait_error = None
        self.cleanup_error = None
        self.bad_session = False
        self.bad_result = False
        self.sink = None

    def start(self, request: AgentRunRequest) -> RuntimeOperationResult:
        self.requests.append(("start", request))
        if self.start_error:
            raise self.start_error
        session = RuntimeSessionRef("fake-non-codex", "session", request.run_id, str(request.workspace_path))
        if self.sink:
            self.sink(AgentEvent("live", request.run_id, "fake-non-codex", "message.delta", {"text": "实时"}))
        return RuntimeOperationResult("ok", None if self.bad_session else session, "turn")

    def resume(self, request: AgentRunRequest) -> RuntimeOperationResult:
        self.requests.append(("resume", request))
        if self.resume_error:
            return RuntimeOperationResult("failed", error=RuntimeFailure(self.resume_error, "恢复失败"))
        return self.start(request)

    def wait(self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float) -> AgentRunResult:
        assert timeout_seconds > 0
        if self.wait_error:
            raise self.wait_error
        return AgentRunResult(
            0, "other" if self.bad_result else session.session_id, "非 Codex 输出", "",
            runtime_id="fake-non-codex", run_id=session.run_id, summary="候选完成",
        )

    def close(self) -> None:
        self.closed = True
        if self.cleanup_error:
            raise self.cleanup_error


def executor(tmp_path, runtime):
    store = RuntimeEventStore(tmp_path / "events")

    def factory(sink):
        runtime.sink = sink
        return runtime

    return RuntimeExecutor(factory, store), store


def test_non_codex_runtime_returns_normalized_result_and_live_events(tmp_path):
    runtime = FakeRuntime()
    bridge, store = executor(tmp_path, runtime)
    result = bridge.execute(tmp_path, "任务", model="custom", sandbox="read-only")
    assert result.returncode == 0
    assert result.runtime_id == "fake-non-codex"
    assert result.summary == "候选完成"
    assert not result.resumed
    assert runtime.closed
    assert store.replay(result.run_id)[0].payload == {"text": "实时"}
    request = runtime.requests[0][1]
    assert request.model == "custom" and request.sandbox == "read-only"
    assert request.workspace_path == tmp_path.resolve()


def test_only_explicit_missing_session_can_fallback(tmp_path):
    runtime = FakeRuntime()
    runtime.resume_error = "session_missing"
    bridge, _ = executor(tmp_path, runtime)
    result = bridge.execute(tmp_path, "任务", resume_session_id="old")
    assert result.returncode == 0 and not result.resumed
    assert [name for name, _ in runtime.requests] == ["resume", "start"]
    assert runtime.requests[-1][1].resume_session_id is None
    assert runtime.closed


@pytest.mark.parametrize("code", ["timeout", "permission_denied", "transport_error", "unknown", "model_unavailable"])
def test_ambiguous_resume_failure_never_replays_prompt(tmp_path, code):
    runtime = FakeRuntime()
    runtime.resume_error = code
    bridge, _ = executor(tmp_path, runtime)
    result = bridge.execute(tmp_path, "不能重放", resume_session_id="old")
    assert result.returncode != 0
    assert [name for name, _ in runtime.requests] == ["resume"]
    assert result.error.code == code
    assert runtime.closed


@pytest.mark.parametrize("field", ["start_error", "wait_error", "cleanup_error"])
def test_adapter_exceptions_fail_closed_and_close(tmp_path, field):
    runtime = FakeRuntime()
    setattr(runtime, field, RuntimeError("故障"))
    bridge, _ = executor(tmp_path, runtime)
    result = bridge.execute(tmp_path, "任务")
    assert result.returncode != 0 and result.error
    assert runtime.closed
    if field != "start_error":
        assert result.session_id == "session"
        assert result.runtime_id == "fake-non-codex"


def test_wait_failure_preserves_resumed_session_identity(tmp_path):
    runtime = FakeRuntime()
    runtime.wait_error = OSError("连接断开")
    bridge, _ = executor(tmp_path, runtime)
    result = bridge.execute(tmp_path, "任务", resume_session_id="session")
    assert result.returncode != 0 and result.resumed
    assert result.session_id == "session"
    assert result.runtime_id == "fake-non-codex" and result.run_id


def test_factory_exception_is_structured_failure(tmp_path):
    def factory(sink):
        raise OSError("未安装")

    bridge = RuntimeExecutor(factory, RuntimeEventStore(tmp_path / "events"))
    result = bridge.execute(tmp_path, "任务")
    assert result.returncode != 0 and result.error.code == "runtime_failure"


@pytest.mark.parametrize("attribute", ["bad_session", "bad_result"])
def test_scope_and_reference_mismatch_never_succeed(tmp_path, attribute):
    runtime = FakeRuntime()
    setattr(runtime, attribute, True)
    bridge, _ = executor(tmp_path, runtime)
    result = bridge.execute(tmp_path, "任务")
    assert result.returncode != 0
    assert runtime.closed


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan")])
def test_invalid_timeout_never_starts_runtime(tmp_path, timeout):
    runtime = FakeRuntime()
    bridge, _ = executor(tmp_path, runtime)
    bridge.timeout_seconds = timeout
    with pytest.raises(ValueError, match="有限正数"):
        bridge.execute(tmp_path, "任务")
    assert runtime.requests == []


def test_event_scope_mismatch_fails_operation(tmp_path):
    class BadEventRuntime(FakeRuntime):
        def start(self, request):
            return super().start(replace(request, run_id="other-run"))

    runtime = BadEventRuntime()
    bridge, store = executor(tmp_path, runtime)
    result = bridge.execute(tmp_path, "任务")
    assert result.returncode != 0
    assert not store.replay("other-run")
    assert runtime.closed
