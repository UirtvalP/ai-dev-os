"""Claude CLI 的真实 JSONL 双向控制、只读边界与失败测试。"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from workspace_orchestrator.agent_runtime.claude import ClaudeCliRuntime
from workspace_orchestrator.agent_runtime.contracts import (
    AgentEvent,
    AgentRunRequest,
    RuntimeSessionRef,
)

FIXTURE = Path(__file__).parent / "fixtures" / "runtime_claude_server.py"


def runtime(mode: str = "normal", events: list[AgentEvent] | None = None) -> ClaudeCliRuntime:
    return ClaudeCliRuntime([sys.executable, "-u", str(FIXTURE), mode],
                            event_sink=events.append if events is not None else None,
                            timeout_seconds=2)


def request(tmp_path: Path, **kwargs: object) -> AgentRunRequest:
    values = {"run_id": "claude-run", "workspace_path": tmp_path,
              "prompt": "safe read", "sandbox": "read-only", "timeout_seconds": 3, **kwargs}
    return AgentRunRequest(**values)  # type: ignore[arg-type]


def test_claude_streamed_turn_models_and_followup(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    adapter = runtime(events=events)
    try:
        opened = adapter.start(request(tmp_path, model="model-b"))
        assert opened.ok and opened.session and opened.turn_id
        done = adapter.wait(opened.session, opened.turn_id, timeout_seconds=3)
        assert done.returncode == 0 and done.summary == "streamed 中文"
        assert {model.id for model in adapter.describe().models} == {"model-a", "model-b"}
        assert adapter.describe().version == "fixture-1"
        assert {"session", "message", "approval", "completion", "unknown"} <= {event.kind for event in events}
        assert {"approval.requested", "approval.resolved"} <= {
            event.extra["detail_kind"] for event in events
        }
        assert all(event.run_id == "claude-run" for event in events)
        assert any(event.payload.get("nested") == {"retained": [1, 2]} for event in events)
        following = adapter.send_message(opened.session, "second turn")
        assert following.ok and following.turn_id and following.turn_id != opened.turn_id
        assert adapter.wait(opened.session, following.turn_id, timeout_seconds=3).returncode == 0
        assert adapter.steer(opened.session, following.turn_id, "x").status == "unsupported"
        assert adapter.archive(opened.session).status == "unsupported"
        assert adapter.read_session(opened.session).status == "unsupported"
        assert adapter.respond_to_request(opened.session, "approval-1", {"allow": True}).status == "unsupported"
    finally:
        adapter.close()


def test_claude_interrupt_ack_is_not_completion(tmp_path: Path) -> None:
    streamed = threading.Event()
    adapter = runtime("hold")
    adapter.event_sink = lambda event: streamed.set() if event.kind == "message" else None
    try:
        opened = adapter.start(request(tmp_path))
        assert opened.ok and opened.session and opened.turn_id
        assert streamed.wait(2)
        assert adapter.wait(opened.session, opened.turn_id, timeout_seconds=0.01).returncode == 124
        assert adapter.send_message(opened.session, "not steer").error.code == "busy"
        cancelled = adapter.interrupt(opened.session, opened.turn_id)
        assert cancelled.ok and cancelled.data["acknowledged"] and not cancelled.data["completed"]
        result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=2)
        assert result.returncode != 0 and result.error
    finally:
        adapter.close()


def test_claude_resume_and_restricted_cli_args(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    adapter = runtime(events=events)
    try:
        opened = adapter.resume(request(tmp_path, resume_session_id="previous-session"))
        assert opened.ok and opened.session and opened.session.session_id == "previous-session"
        assert opened.turn_id
        assert adapter.wait(opened.session, opened.turn_id, timeout_seconds=3).resumed
        args = next(item.payload["argv"] for item in events if item.kind == "session")
        assert "--resume=previous-session" in args
        assert args[args.index("--tools") + 1] == "Read,Grep,Glob"
        assert args[args.index("--permission-mode") + 1] == "plan"
        assert args[args.index("--permission-prompt-tool") + 1] == "stdio"
        assert "--strict-mcp-config" in args
        assert "--dangerously-skip-permissions" not in args
    finally:
        adapter.close()


@pytest.mark.parametrize("mode", ["bad-control", "init-eof", "wrong-session", "model-refused"])
def test_claude_setup_failure_is_not_success(tmp_path: Path, mode: str) -> None:
    adapter = runtime(mode)
    try:
        result = adapter.start(request(tmp_path, model="model-b"))
        assert not result.ok and result.error and result.error.code != "session_missing"
    finally:
        adapter.close()


def test_claude_exit_without_result_is_failure(tmp_path: Path) -> None:
    adapter = runtime("eof")
    try:
        opened = adapter.start(request(tmp_path))
        assert opened.session and opened.turn_id
        result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=3)
        assert result.returncode != 0 and result.error
    finally:
        adapter.close()


def test_claude_missing_unsupported_unknown_model_and_foreign_session(tmp_path: Path) -> None:
    missing = ClaudeCliRuntime(executable="ai-dev-os-claude-not-installed")
    assert not missing.describe().available
    assert missing.start(request(tmp_path)).status == "unavailable"
    adapter = runtime()
    try:
        assert adapter.start(request(tmp_path, sandbox="workspace-write")).status == "unsupported"
        assert adapter.start(request(tmp_path, sandbox="unconfined")).status == "unsupported"
        assert adapter.start(request(tmp_path, bypass_hook_trust=True)).status == "unsupported"
        assert not adapter.send_message(RuntimeSessionRef("claude", "foreign"), "x").ok
        assert adapter.start(request(tmp_path, model="undiscovered")).status == "unsupported"
    finally:
        adapter.close()


def test_claude_strips_parent_identity_and_preserves_explicit_resume_option(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    env = {**os.environ, "CODEX_THREAD_ID": "root", "CODEX_SESSION_ID": "root",
           "CLAUDECODE": "1", "CLAUDE_SESSION_ID": "root"}
    adapter = ClaudeCliRuntime([sys.executable, "-u", str(FIXTURE), "normal"],
                              event_sink=events.append, environ=env, timeout_seconds=2)
    try:
        opened = adapter.resume(request(tmp_path, resume_session_id="--dangerously-skip-permissions"))
        assert opened.session and opened.turn_id
        assert adapter.wait(opened.session, opened.turn_id, timeout_seconds=3).returncode == 0
        init = next(event.payload for event in events if event.kind == "session")
        assert all(value is None for value in init["parent_env"].values())
        assert "--resume=--dangerously-skip-permissions" in init["argv"]
        assert "--dangerously-skip-permissions" not in init["argv"]
    finally:
        adapter.close()


def test_claude_terminal_event_sink_failure_is_not_completion(tmp_path: Path) -> None:
    adapter = runtime()

    def sink(event: AgentEvent) -> None:
        if event.kind == "completion":
            raise OSError("event store unavailable")

    adapter.event_sink = sink
    try:
        opened = adapter.start(request(tmp_path))
        if opened.ok:
            assert opened.session and opened.turn_id
            result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=3)
            assert result.returncode != 0 and result.error
        else:
            assert opened.error
    finally:
        adapter.close()
