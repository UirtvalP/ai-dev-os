from pathlib import Path

import pytest

from workspace_orchestrator.agent_runtime.contracts import (
    AgentRunResult,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from workspace_orchestrator.dashboard import CommandQueue
from workspace_orchestrator.deployment import (
    DeploymentAuthorization,
    DeploymentError,
    DeploymentPolicy,
    DeploymentService,
)
from workspace_orchestrator.integration.contracts import MergeReceipt
from workspace_orchestrator.remote_control import (
    _DASHBOARD_HTML,
    RemoteController,
    load_or_create_token,
)
from workspace_orchestrator.workspace import WorkspaceStore


def test_command_queue_is_persistent_and_idempotent(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path / "commands.json")
    first = queue.enqueue("REQ-020", "session-1", "继续", command_id="cmd-1")
    assert queue.enqueue("REQ-020", "session-1", "继续", command_id="cmd-1") == first
    assert CommandQueue(tmp_path / "commands.json").pending("session-1") == (first,)
    assert queue.update("cmd-1", "completed", "ok").status == "completed"
    assert queue.history(session_id="session-1")[-1].result == "ok"


class FakeRuntime:
    def __init__(self, event_sink: object) -> None:
        self.event_sink = event_sink
        self.session = RuntimeSessionRef("codex", "session-1", "remote-test", "")

    def resume(self, request: object) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", self.session, "turn-1")

    def start(self, request: object) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", self.session, "turn-new")

    def send_message(self, session: RuntimeSessionRef, text: str) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", self.session, "turn-2")

    def wait(self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float) -> AgentRunResult:
        return AgentRunResult(0, session.session_id, "收到", "", runtime_id="codex")

    def read_session(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", session, data={"thread": {"id": session.session_id}})

    def close(self) -> None:
        pass


def test_remote_controller_uses_fixed_read_only_session(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程控制", task_provider=None)
    made: list[FakeRuntime] = []

    def factory(name: str, *, event_sink: object) -> FakeRuntime:
        assert name == "codex"
        made.append(FakeRuntime(event_sink))
        return made[-1]

    controller = RemoteController(
        store, "REQ-001", "session-1", run_id="remote-test", runtime_factory=factory,
    )
    assert controller.message("只回复 READY", command_id="cmd-remote")["result"] == "收到"
    status = controller.status()
    assert status["session_id"] == "session-1"
    assert status["safety"] == {"sandbox": "read-only", "approvals": "deny"}
    assert status["thread"]["id"] == "session-1"


def test_dashboard_token_is_generated_and_reused(tmp_path: Path) -> None:
    token_path = tmp_path / "dashboard.token"
    first = load_or_create_token(token_path)
    assert len(first.encode()) >= 32
    assert load_or_create_token(token_path) == first


def test_dashboard_browser_api_paths_support_reverse_proxy_prefix() -> None:
    assert "request('api/status')" in _DASHBOARD_HTML
    assert "request('api/message'" in _DASHBOARD_HTML
    assert "request('/api/" not in _DASHBOARD_HTML


def test_remote_controller_can_create_dedicated_session(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程控制", task_provider=None)

    def factory(name: str, *, event_sink: object) -> FakeRuntime:
        return FakeRuntime(event_sink)

    controller = RemoteController(
        store, "REQ-001", None, run_id="remote-new", runtime_factory=factory,
    )
    assert controller.message("只回复 READY", command_id="cmd-new")["status"] == "completed"
    assert controller.status()["session_id"] == "session-1"


class Main:
    def __init__(self, sha: str, branch: str = "main") -> None:
        self.sha, self.branch = sha, branch

    def state(self) -> tuple[str, str | None, bool, str]:
        return self.branch, self.sha, True, self.sha


class Provider:
    calls = 0

    def deploy(self, *, environment: str, commit_sha: str) -> tuple[bool, str, str]:
        self.calls += 1
        return True, "ok", "rollback"


def test_deployment_is_main_only_and_idempotent(tmp_path: Path) -> None:
    sha, tree = "a" * 40, "b" * 40
    merge = MergeReceipt("merge-1", "REQ-020", "request-1", "merged", "c" * 40,
                         sha, tree, "refs/heads/integration", "integration-auth",
                         "pre", "post", "2026-09-07T00:00:00+00:00")
    auth = DeploymentAuthorization("deploy-auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = Provider()
    service = DeploymentService(tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider)
    first = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    second = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert first == second and provider.calls == 1
    assert service.complete("REQ-020", sha, deployment_required=True,
                            receipt=first).deployment_receipt_id == first.receipt_id
    assert service.complete("REQ-020", sha, deployment_required=False).deployment_receipt_id is None

    blocked = DeploymentService(tmp_path / "blocked", DeploymentPolicy("prod", "1"),
                                Main(sha, "feature"), provider)
    with pytest.raises(DeploymentError, match="main"):
        blocked.deploy(auth, merge, post_merge_verification_receipt_id="post")
