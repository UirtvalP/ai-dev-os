"""真实协议端的无 prompt 发现、标准装配路由及独立 Worker 生命周期回归。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

from workspace_orchestrator import composition
from workspace_orchestrator.agent_runtime.claude import ClaudeCliRuntime
from workspace_orchestrator.agent_runtime.contracts import AgentEvent, AgentRunRequest
from workspace_orchestrator.agent_runtime.cursor import CursorAcpRuntime
from workspace_orchestrator.agent_runtime.stdio import JsonRpcStdioClient
from workspace_orchestrator.orchestration.contracts import PolicyError, TaskSpec
from workspace_orchestrator.orchestration.policies import CapabilityModelRouter, validate_route


class RecordingClient(JsonRpcStdioClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[dict[str, Any]] = []
        self.close_calls = 0
        self.refuse_close = False

    def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(copy.deepcopy(message))
        super()._send(message)

    def close(self) -> None:
        self.close_calls += 1
        if self.refuse_close:
            raise OSError("fixture cleanup not confirmed")
        super().close()


def adapter(
    name: str, clients: list[RecordingClient], events: list[AgentEvent], *, mode: str = "normal",
) -> CursorAcpRuntime | ClaudeCliRuntime:
    def factory(*args: Any, **kwargs: Any) -> RecordingClient:
        client = RecordingClient(*args, **kwargs)
        clients.append(client)
        return client

    selected = CursorAcpRuntime if name == "cursor" else ClaudeCliRuntime
    fixture = Path(__file__).parent / "fixtures" / f"runtime_{name}_server.py"
    return selected(
        [sys.executable, "-u", str(fixture), mode], event_sink=events.append,
        timeout_seconds=0.3 if mode == "init-timeout" else 2, client_factory=factory,
    )


def assert_discovery_only(name: str, client: RecordingClient) -> None:
    assert client.cwd and client.cwd.name.startswith(f"ai-dev-os-{name}-discovery-")
    if name == "cursor":
        assert [message.get("method") for message in client.sent] == [
            "initialize", "authenticate", "session/new",
        ]
        assert client.sent[-1]["params"] == {"cwd": str(client.cwd.resolve()), "mcpServers": []}
    else:
        assert len(client.sent) == 1
        assert client.sent[0]["type"] == "control_request"
        assert client.sent[0]["request"] == {"subtype": "initialize", "hooks": None}
        assert "--no-session-persistence" in client.command
        assert client.command[client.command.index("--tools") + 1] == ""
    assert not client.running and client._process and client._process.poll() is not None
    assert not client.cwd.exists() and client.close_calls >= 1


@pytest.mark.parametrize("name", ["cursor", "claude"])
def test_describe_discovers_without_prompt_and_start_reuses_no_probe_state(tmp_path, name):
    clients: list[RecordingClient] = []
    events: list[AgentEvent] = []
    runtime = adapter(name, clients, events)
    try:
        description = runtime.describe()
        assert description.available and description.supports("models")
        assert {model.id for model in description.models} == {"model-a", "model-b"}
        assert all(model.reasoning_efforts == () for model in description.models)
        assert len(clients) == 1 and events == []
        assert runtime._client is None and runtime._request is None and runtime._session is None
        assert runtime._active_turn is None and runtime._raw == {}
        assert_discovery_only(name, clients[0])
        assert runtime.describe() == description and len(clients) == 1

        opened = runtime.start(AgentRunRequest(
            "actual-run", tmp_path, "actual user task", sandbox="read-only", model="model-b",
        ))
        assert opened.ok and opened.session and opened.turn_id
        assert runtime.wait(opened.session, opened.turn_id, timeout_seconds=3).returncode == 0
        assert len(clients) == 2 and clients[1].cwd == tmp_path
        assert runtime._client is clients[1]
        assert events and all(event.run_id == "actual-run" for event in events)
        prompts = [message for message in clients[1].sent
                   if message.get("method") == "session/prompt" or message.get("type") == "user"]
        assert len(prompts) == 1
        assert runtime.describe().available and len(clients) == 2
        if name == "claude":
            assert "--no-session-persistence" not in clients[1].command
            assert clients[1].command[clients[1].command.index("--tools") + 1] == "Read,Grep,Glob"
    finally:
        runtime.close()
    assert not runtime.describe().available and len(clients) == 2
    assert all(client._process and client._process.poll() is not None for client in clients)


@pytest.mark.parametrize("name", ["cursor", "claude"])
def test_direct_start_never_recursively_discovers(tmp_path, name):
    clients: list[RecordingClient] = []
    runtime = adapter(name, clients, [])
    try:
        opened = runtime.start(AgentRunRequest("run", tmp_path, "task", sandbox="read-only"))
        assert opened.ok and opened.session and opened.turn_id
        assert runtime.wait(opened.session, opened.turn_id, timeout_seconds=3).returncode == 0
        assert len(clients) == 1 and clients[0].cwd == tmp_path
        assert runtime.describe().available and len(clients) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("name,mode", [
    ("cursor", "bad-version"), ("cursor", "init-eof"), ("cursor", "init-timeout"),
    ("cursor", "no-models"), ("claude", "bad-control"), ("claude", "init-eof"),
    ("claude", "init-timeout"), ("claude", "no-models"),
])
def test_discovery_failure_is_unavailable_closed_and_never_a_prompt(name, mode):
    clients: list[RecordingClient] = []
    events: list[AgentEvent] = []
    runtime = adapter(name, clients, events, mode=mode)
    try:
        description = runtime.describe()
        assert not description.available and description.models == ()
        assert description.reason and "发现失败" in description.reason
        assert len(clients) == 1 and events == []
        assert clients[0]._process and clients[0]._process.poll() is not None
        assert clients[0].cwd and not clients[0].cwd.exists()
        assert not any(message.get("method") == "session/prompt" or message.get("type") == "user"
                       for message in clients[0].sent)
        assert runtime.describe() == description and len(clients) == 1
        with pytest.raises(PolicyError, match="没有同时满足"):
            CapabilityModelRouter().route(TaskSpec("T", "Title", "Task"), (description,))
    finally:
        runtime.close()


@pytest.mark.parametrize("name", ["cursor", "claude"])
def test_failed_discovery_does_not_poison_later_actual_connection(tmp_path, name):
    clients: list[RecordingClient] = []
    runtime = adapter(name, clients, [], mode="init-eof")
    try:
        assert not runtime.describe().available
        runtime.command = (*runtime.command[:-1], "normal")
        opened = runtime.start(AgentRunRequest("run", tmp_path, "task", sandbox="read-only"))
        assert opened.ok and opened.session and opened.turn_id
        assert runtime.wait(opened.session, opened.turn_id, timeout_seconds=3).returncode == 0
        assert len(clients) == 2 and runtime.describe().available
    finally:
        runtime.close()


@pytest.mark.parametrize("name", ["cursor", "claude"])
def test_discovery_cleanup_failure_blocks_new_tree_and_retains_owner(tmp_path, name):
    clients: list[RecordingClient] = []
    runtime = adapter(name, clients, [])
    original_factory = runtime.client_factory

    def factory(*args, **kwargs):
        client = original_factory(*args, **kwargs)
        client.refuse_close = True
        return client

    runtime.client_factory = factory
    try:
        description = runtime.describe()
        assert not description.available and description.models == ()
        assert "cleanup not confirmed" in description.reason
        assert len(clients) == 1 and clients[0].running and runtime._discovery is not None
        assert not runtime.start(AgentRunRequest("run", tmp_path, "task", sandbox="read-only")).ok
        with pytest.raises(OSError, match="cleanup not confirmed"):
            runtime.close()
        assert len(clients) == 1 and clients[0].running
    finally:
        for client in clients:
            client.refuse_close = False
        runtime.close()
    assert clients[0]._process and clients[0]._process.poll() is not None
    assert clients[0].cwd and not clients[0].cwd.exists()


def test_standard_composition_routes_new_real_cursor_and_claude_adapters(monkeypatch):
    clients: dict[str, list[RecordingClient]] = {"cursor": [], "claude": []}
    events: list[AgentEvent] = []
    instances = []

    def create(name, **kwargs):
        if name == "codex":
            # Keep a real non-target Adapter but prevent any installed CLI use.
            from workspace_orchestrator.agent_runtime.codex import CodexRuntime

            return CodexRuntime(executable="ai-dev-os-fixture-codex-not-installed")
        result = adapter(name, clients[name], events)
        instances.append(result)
        return result

    monkeypatch.setattr(composition, "create_runtime", create)
    descriptions = composition.runtime_descriptors()
    assert len(descriptions) == 3
    assert not descriptions[0].available
    for name in ("cursor", "claude"):
        task = TaskSpec("T", "Title", "Task", preferred_runtime=name, preferred_model="model-b")
        route, decision = CapabilityModelRouter().route(task, descriptions)
        assert route.runtime_id == name and route.model == "model-b" and route.effort is None
        assert decision.decision == route.to_dict()
        validate_route(task, route, descriptions)
        assert len(clients[name]) == 1
        assert_discovery_only(name, clients[name][0])
    assert events == [] and all(instance._closed for instance in instances)


def test_standard_composition_discovery_failure_does_not_hide_other_runtime(monkeypatch):
    clients: list[RecordingClient] = []

    def create(name, **kwargs):
        if name == "codex":
            from workspace_orchestrator.agent_runtime.codex import CodexRuntime

            return CodexRuntime(executable="ai-dev-os-fixture-codex-not-installed")
        return adapter(name, clients, [], mode="bad-version" if name == "cursor" else "normal")

    monkeypatch.setattr(composition, "create_runtime", create)
    descriptions = composition.runtime_descriptors()
    assert not descriptions[1].available and descriptions[2].available
    route, _ = CapabilityModelRouter().route(TaskSpec("T", "Title", "Task"), descriptions)
    assert route.runtime_id == "claude"
    assert all(client._process and client._process.poll() is not None for client in clients)


@pytest.mark.parametrize("name", ["cursor", "claude"])
def test_discovered_models_cannot_substitute_actual_session_negotiation(tmp_path, name):
    clients: list[RecordingClient] = []
    runtime = adapter(name, clients, [])
    try:
        assert runtime.describe().available
        runtime.command = (*runtime.command[:-1], "no-models")
        opened = runtime.start(AgentRunRequest(
            "run", tmp_path, "never send task", sandbox="read-only", model="model-b",
        ))
        assert opened.status == "unsupported" and len(clients) == 2
        assert not any(message.get("method") == "session/prompt" or message.get("type") == "user"
                       for client in clients for message in client.sent)
    finally:
        runtime.close()


@pytest.mark.parametrize("name", ["cursor", "claude"])
def test_closed_adapter_describe_does_not_start_discovery(name):
    clients: list[RecordingClient] = []
    runtime = adapter(name, clients, [])
    runtime.close()
    assert not runtime.describe().available and clients == []
