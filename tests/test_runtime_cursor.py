"""Cursor ACP 的真实 JSONL 边界、能力协商和失败闭锁测试。"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from workspace_orchestrator.agent_runtime.contracts import (
    AgentEvent,
    AgentRunRequest,
    RuntimeSessionRef,
)
from workspace_orchestrator.agent_runtime.cursor import CursorAcpRuntime

FIXTURE = Path(__file__).parent / "fixtures" / "runtime_cursor_server.py"


def runtime(mode: str = "normal", events: list[AgentEvent] | None = None) -> CursorAcpRuntime:
    return CursorAcpRuntime([sys.executable, "-u", str(FIXTURE), mode],
                            event_sink=events.append if events is not None else None,
                            timeout_seconds=2)


def request(tmp_path: Path, **kwargs: object) -> AgentRunRequest:
    values = {"run_id": "cursor-run", "workspace_path": tmp_path,
              "prompt": "safe read", "sandbox": "read-only", "timeout_seconds": 3, **kwargs}
    return AgentRunRequest(**values)  # type: ignore[arg-type]


def test_cursor_streamed_turn_message_and_capabilities(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    adapter = runtime(events=events)
    try:
        assert {model.id for model in adapter.describe().models} == {"model-a", "model-b"}
        assert adapter.describe().supports("resume")
        opened = adapter.start(request(tmp_path, model="model-b"))
        assert opened.ok and opened.session and opened.turn_id
        done = adapter.wait(opened.session, opened.turn_id, timeout_seconds=3)
        assert done.returncode == 0 and done.summary == "streamed 中文"
        assert adapter.describe().supports("resume")
        assert any(model.id == "model-b" and model.is_default for model in adapter.describe().models)
        assert {"session", "message", "tool", "approval", "completion", "unknown"} <= {item.kind for item in events}
        assert {"tool.started", "tool.updated", "approval.requested", "approval.resolved"} <= {
            item.extra["detail_kind"] for item in events
        }
        unknown = next(item for item in events if item.kind == "unknown")
        assert unknown.payload["params"]["update"]["future"] == {"unchanged": [1, {"nested": True}]}
        assert all(item.run_id == "cursor-run" for item in events)
        following = adapter.send_message(opened.session, "second turn")
        assert following.ok and following.turn_id != opened.turn_id and following.turn_id
        assert adapter.wait(opened.session, following.turn_id, timeout_seconds=3).returncode == 0
        assert adapter.steer(opened.session, following.turn_id, "steer").status == "unsupported"
        assert adapter.archive(opened.session).status == "unsupported"
        assert adapter.read_session(opened.session).status == "unsupported"
        assert adapter.respond_to_request(opened.session, "permission-1", {"allow": True}).status == "unsupported"
    finally:
        adapter.close()


def test_cursor_stream_visible_before_cancel_confirmation(tmp_path: Path) -> None:
    streamed = threading.Event()
    adapter = runtime("hold")
    adapter.event_sink = lambda event: streamed.set() if event.kind == "message" else None
    try:
        opened = adapter.start(request(tmp_path))
        assert opened.ok and opened.session and opened.turn_id
        assert streamed.wait(2)
        assert adapter.send_message(opened.session, "not steer").error.code == "busy"
        assert adapter.wait(opened.session, opened.turn_id, timeout_seconds=0.01).returncode == 124
        cancelled = adapter.interrupt(opened.session, opened.turn_id)
        assert cancelled.ok and cancelled.data == {"requested": True, "confirmed": False}
        result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=2)
        assert result.returncode == 130 and result.error and result.error.code == "cancelled"
    finally:
        adapter.close()


def test_cursor_resume_replays_null_response_and_honors_capability(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    adapter = runtime(events=events)
    try:
        opened = adapter.resume(request(tmp_path, resume_session_id="previous-session"))
        assert opened.ok and opened.session and opened.session.session_id == "previous-session"
        assert opened.turn_id
        assert adapter.wait(opened.session, opened.turn_id, timeout_seconds=3).resumed
        assert any(event.payload.get("params", {}).get("update", {}).get("content", {}).get("text")
                   == "historic reply" for event in events)
    finally:
        adapter.close()
    adapter = runtime("no-resume")
    try:
        assert adapter.resume(request(tmp_path, resume_session_id="previous")).status == "unsupported"
    finally:
        adapter.close()


@pytest.mark.parametrize("mode", ["bad-version", "model-refused", "resume-error"])
def test_cursor_setup_failure_is_not_success_or_replayed(tmp_path: Path, mode: str) -> None:
    adapter = runtime(mode)
    try:
        result = (adapter.resume(request(tmp_path, resume_session_id="previous"))
                  if mode == "resume-error" else adapter.start(request(tmp_path, model="model-b")))
        assert not result.ok and result.error
        assert result.error.code != "session_missing"
    finally:
        adapter.close()


@pytest.mark.parametrize("mode", ["eof", "wrong-session", "bad-stop"])
def test_cursor_stream_corruption_is_not_completion(tmp_path: Path, mode: str) -> None:
    adapter = runtime(mode)
    try:
        opened = adapter.start(request(tmp_path))
        assert opened.session and opened.turn_id
        result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=3)
        assert result.returncode != 0 and result.error
    finally:
        adapter.close()


def test_cursor_missing_unsupported_and_foreign_session(tmp_path: Path) -> None:
    missing = CursorAcpRuntime(executable="ai-dev-os-cursor-not-installed")
    assert not missing.describe().available
    assert missing.start(request(tmp_path)).status == "unavailable"
    adapter = runtime()
    try:
        assert adapter.start(request(tmp_path, sandbox="workspace-write")).status == "unsupported"
        assert adapter.start(request(tmp_path, sandbox="unconfined")).status == "unsupported"
        assert adapter.start(request(tmp_path, bypass_hook_trust=True)).status == "unsupported"
        assert not adapter.send_message(RuntimeSessionRef("cursor", "foreign"), "x").ok
    finally:
        adapter.close()


def test_cursor_strips_parent_session_identity(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    env = {**os.environ, "CODEX_THREAD_ID": "root", "CODEX_SESSION_ID": "root",
           "CLAUDECODE": "1", "CLAUDE_SESSION_ID": "root"}
    adapter = CursorAcpRuntime([sys.executable, "-u", str(FIXTURE), "normal"],
                              event_sink=events.append, environ=env, timeout_seconds=2)
    try:
        opened = adapter.start(request(tmp_path))
        assert opened.session and opened.turn_id
        assert adapter.wait(opened.session, opened.turn_id, timeout_seconds=3).returncode == 0
        unknown = next(item for item in events if item.kind == "unknown")
        assert all(value is None for value in unknown.payload["params"]["update"]["parent_env"].values())
    finally:
        adapter.close()


def test_cursor_timeout_closes_connection_instead_of_allowing_replay(tmp_path: Path) -> None:
    adapter = runtime("hold")
    try:
        opened = adapter.start(request(tmp_path, timeout_seconds=0.1))
        assert opened.session and opened.turn_id
        result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=3)
        assert result.returncode == 124 and result.error and result.error.code == "timeout"
        assert not adapter.send_message(opened.session, "do not replay").ok
    finally:
        adapter.close()


def test_cursor_terminal_event_sink_failure_does_not_hang_or_succeed(tmp_path: Path) -> None:
    adapter = runtime()

    def sink(event: AgentEvent) -> None:
        if event.kind == "completion":
            raise OSError("event store unavailable")

    adapter.event_sink = sink
    try:
        opened = adapter.start(request(tmp_path))
        assert opened.session and opened.turn_id
        result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=3)
        assert result.returncode != 0 and result.error and result.error.code == "event_sink_error"
    finally:
        adapter.close()
