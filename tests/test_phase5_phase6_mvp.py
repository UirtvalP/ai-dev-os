import json
import socket
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from workspace_orchestrator.agent_runtime.contracts import (
    AgentEvent,
    AgentRunResult,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
from workspace_orchestrator.dashboard import CommandQueue, DashboardService
from workspace_orchestrator.dashboard_ui import DASHBOARD_HTML
from workspace_orchestrator.delivery_guard import mark_v2_delivery, require_delivery_completion
from workspace_orchestrator.deployment import (
    CompletionToken,
    DeploymentAuthority,
    DeploymentAuthorityStore,
    DeploymentAuthorization,
    DeploymentEnvironment,
    DeploymentEnvironmentRegistry,
    DeploymentError,
    DeploymentPolicy,
    DeploymentReceipt,
    DeploymentService,
    DryRunDeploymentProvider,
    publish_completion_token,
)
from workspace_orchestrator.deployment_adapters import LocalGitMainStateProvider
from workspace_orchestrator.executions import ExecutionStore
from workspace_orchestrator.integration.contracts import MergeReceipt
from workspace_orchestrator.main_agent import RequirementOwner
from workspace_orchestrator.remote_control import (
    RemoteController,
    load_or_create_token,
    serve_remote_control,
)
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def bind_session(store: WorkspaceStore, requirement_id: str, session_id: str) -> None:
    store.write_json(store.path_for(requirement_id) / "sessions.json", [{
        "id": session_id,
        "agent": "codex",
        "result": "in_progress",
        "task_ids": [],
        "started_at": "2026-09-07T00:00:00+00:00",
    }])


def test_command_queue_is_persistent_and_idempotent(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path / "commands.json")
    first = queue.enqueue("REQ-020", "session-1", "继续", command_id="cmd-1")
    assert queue.enqueue("REQ-020", "session-1", "继续", command_id="cmd-1") == first
    assert CommandQueue(tmp_path / "commands.json").pending("session-1") == (first,)
    assert queue.update("cmd-1", "completed", "ok").status == "completed"
    assert queue.history(session_id="session-1")[-1].result == "ok"
    failed = queue.enqueue("REQ-020", "session-1", "再试", command_id="cmd-failed")
    assert failed.created_at
    assert queue.update(failed.command_id, "failed", "暂时失败").completed_at
    retried = queue.retry(failed.command_id, retry_id="cmd-retry")
    assert retried.retry_of == failed.command_id and retried.attempt == 2
    cancelled = queue.enqueue("REQ-020", "session-1", "取消", command_id="cmd-cancel")
    assert queue.cancel(cancelled.command_id).status == "cancelled"


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
        return RuntimeOperationResult("ok", session, data={"thread": {
            "id": session.session_id,
            "tokenUsage": {"total": 12},
            "turns": [{
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {"id": "m1", "type": "userMessage", "text": "开始"},
                    {"id": "m2", "type": "agentMessage", "text": "收到"},
                    {"id": "t1", "type": "commandExecution", "command": "pytest"},
                    {"id": "f1", "type": "fileChange", "path": "a.py"},
                    {"id": "a1", "type": "approvalRequest", "text": "审批"},
                ],
            }],
        }})

    def steer(
        self, session: RuntimeSessionRef, turn_id: str, text: str,
    ) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", session, turn_id)

    def interrupt(
        self, session: RuntimeSessionRef, turn_id: str,
    ) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", session, turn_id)

    def close(self) -> None:
        pass


def test_remote_controller_uses_fixed_read_only_session(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程控制", task_provider=None)
    bind_session(store, "REQ-001", "session-1")
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
    assert [item["role"] for item in status["agent_detail"]["messages"]] == [
        "user", "agent",
    ]
    assert status["agent_detail"]["tools"][0]["name"] == "pytest"
    assert status["agent_detail"]["file_changes"][0]["path"] == "a.py"
    assert status["agent_detail"]["approvals"][0]["text"] == "审批"
    assert status["agent_detail"]["usage"] == {"total": 12}


def test_dashboard_token_is_generated_and_reused(tmp_path: Path) -> None:
    token_path = tmp_path / "dashboard.token"
    first = load_or_create_token(token_path)
    assert len(first.encode()) >= 32
    assert load_or_create_token(token_path) == first


def test_dashboard_browser_api_paths_support_reverse_proxy_prefix() -> None:
    assert "api('api/status" in DASHBOARD_HTML
    assert "api('api/message'" in DASHBOARD_HTML
    assert "api('/api/" not in DASHBOARD_HTML
    assert 'aria-label="控制面导航"' in DASHBOARD_HTML
    assert 'aria-live="polite"' in DASHBOARD_HTML
    assert "api/commands/${id}/${action}" in DASHBOARD_HTML
    assert "api/executions/${encodeURIComponent(executionId)}" in DASHBOARD_HTML
    for label in (
        "Intent / Acceptance / Progress", "Main Agent", "Active Agents / Executions",
        "Task Graph", "Provider / Runtime", "Model / Reasoning", "Conversation",
        "Tool calls", "Commands", "Files", "Diff", "Tests", "Errors", "Events",
    ):
        assert label in DASHBOARD_HTML
    assert "projection||{},executions=(p.executions||[])" in DASHBOARD_HTML


def test_dashboard_projects_phase_gate_and_workspace_agents(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    requirement_id = store.create("控制面投影", task_provider=None)
    definition_root = root / ".ai-dev-os" / "gate-definitions" / requirement_id
    plan = root / ".ai-dev-os" / "plans" / requirement_id / "phase-0.md"
    store.write_text(plan, "# 0. 基线与主计划（AID-001）")
    store.write_json(definition_root / "phase-0.json", {
        "phase": 0,
        "task_id": "AID-001",
        "plan_source_path": f".ai-dev-os/plans/{requirement_id}/phase-0.md",
        "acceptance": [{"id": "P0-AC-01", "description": "基线通过"}],
    })
    store.write_json(store.path_for(requirement_id) / "phase-gates" / "phase-0.json", {
        "status": "PASS",
        "commit_sha": "a" * 40,
        "acceptance_results": [{"acceptance_id": "P0-AC-01", "status": "PASS"}],
    })
    store.write_json(store.path_for(requirement_id) / "sessions.json", [{
        "id": "session-1",
        "agent": "codex",
        "result": "in_progress",
        "task_ids": [],
        "started_at": "2026-09-07T00:00:00+00:00",
    }])
    service = DashboardService(
        store,
        RuntimeEventStore(store.root / "runtime-events"),
        CommandQueue(store.path_for(requirement_id) / "dashboard" / "commands.json"),
    )

    projection = service.requirement(requirement_id)["projection"]

    assert projection["phases"][0] == {
        "phase": 0,
        "task_id": "AID-001",
        "title": "基线与主计划",
        "status": "passed",
        "commit_sha": "a" * 40,
        "activated_at": None,
        "acceptance": [{"id": "P0-AC-01", "description": "基线通过", "status": "PASS"}],
        "passed_acceptance": 1,
        "total_acceptance": 1,
    }
    assert projection["agents"][0]["session_id"] == "session-1"
    assert projection["agents"][0]["role"] == "Control"


def test_dashboard_projects_requirement_space_main_agent_and_execution_details(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    requirement_id = store.create(
        "独立工作台", goal="从 Requirement 驱动执行", acceptance=["显示 Execution", "保留事件"],
    )
    owner = RequirementOwner(store, requirement_id)
    owner.observe(expected_revision=0)
    executions = ExecutionStore(store)
    parent = executions.create(
        requirement_id, "TASK-1", role="implementation", runtime_id="codex",
        model="discovered", reasoning_effort="high", prompt="实现",
    )
    parent = executions.update(
        parent.id, status="completed", session_id="session-1", started_at="2026-09-07T00:00:00+00:00",
        completed_at="2026-09-07T00:01:30+00:00", summary="完成",
    )
    child = executions.create(
        requirement_id, "TASK-2", role="reviewer", runtime_id="claude", prompt="审查",
        parent_execution_id=parent.id,
    )
    events = RuntimeEventStore(store.root / "runtime-events")
    events.append(AgentEvent(
        "message-1", parent.id, "codex", "message", {"text": "完成实现"},
        session_id="session-1", task_id="TASK-1", requirement_id=requirement_id,
        execution_id=parent.id,
    ))
    events.append(AgentEvent(
        "tool-1", parent.id, "codex", "tool", {"name": "pytest", "path": "tests/test_a.py"},
        session_id="session-1", task_id="TASK-1", requirement_id=requirement_id,
        execution_id=parent.id,
    ))
    queue = CommandQueue(store.path_for(requirement_id) / "dashboard" / "commands.json")
    queue.enqueue(requirement_id, "session-1", "继续", command_id="cmd-1")

    service = DashboardService(store, events, queue)
    projection = service.requirement(requirement_id)["projection"]

    assert projection["requirement_space"]["goal"] == "从 Requirement 驱动执行"
    assert projection["requirement_space"]["progress"] == {
        "executions_total": 2, "executions_terminal": 1,
        "acceptance_total": 2, "acceptance_completed": 0,
    }
    assert projection["main_agent"]["requirement_id"] == requirement_id
    assert projection["task_graph"] == {"nodes": [], "edges": [], "source": "supervisor"}
    assert [node["execution_id"] for node in projection["execution_graph"]["nodes"]] == [
        parent.id, child.id,
    ]
    assert projection["execution_graph"]["edges"] == [{
        "from_execution": parent.id, "to_execution": child.id,
    }]
    summary = next(item for item in projection["executions"] if item["id"] == parent.id)
    assert summary["provider"] == "codex" and summary["duration_seconds"] == 90
    assert "details" not in summary and "prompt" not in summary
    page = service.execution(requirement_id, parent.id)
    assert page["details"]["conversation"][0]["payload"] == {"text": "完成实现"}
    assert page["details"]["tool_calls"][0]["payload"]["name"] == "pytest"
    assert page["details"]["files"][0]["event_id"] == "tool-1"
    assert page["details"]["tests"][0]["event_id"] == "tool-1"
    assert page["details"]["commands"][0]["command_id"] == "cmd-1"
    assert page["command_scope"] == "session"
    assert len(page["details"]["events"]) == 2 and page["has_more"] is False


def test_dashboard_execution_details_are_paginated_and_validate_all_identities(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Execution page")
    execution = ExecutionStore(store).create(
        requirement_id, "TASK-1", role="worker", runtime_id="codex", prompt="run",
    )
    execution = ExecutionStore(store).update(
        execution.id, status="running", session_id="session-1",
    )
    events = RuntimeEventStore(store.root / "runtime-events")
    for index in range(3):
        events.append(AgentEvent(
            f"event-{index}", execution.id, "codex", "message", {"index": index},
            session_id="session-1", task_id="TASK-1", requirement_id=requirement_id,
            execution_id=execution.id,
        ))
    service = DashboardService(
        store, events, CommandQueue(store.path_for(requirement_id) / "dashboard" / "commands.json"),
    )
    overview = service.requirement(requirement_id)["projection"]["executions"][0]
    assert "details" not in overview
    first = service.execution(requirement_id, execution.id, limit=2)
    assert [item["event_id"] for item in first["details"]["events"]] == ["event-0", "event-1"]
    assert first["has_more"] is True and first["next_cursor"] == 2
    second = service.execution(requirement_id, execution.id, after=2, limit=2)
    assert [item["event_id"] for item in second["details"]["events"]] == ["event-2"]
    assert second["has_more"] is False

    events.append(AgentEvent(
        "cross-requirement", execution.id, "codex", "message", {"secret": "other"},
        session_id="session-1", task_id="TASK-1", requirement_id="REQ-999",
        execution_id=execution.id,
    ))
    with pytest.raises(WorkspaceError, match="身份不匹配"):
        service.execution(requirement_id, execution.id, after=3)


def test_dashboard_marks_main_agent_stale_when_non_phase_source_changes(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Owner freshness", goal="before")
    owner = RequirementOwner(store, requirement_id)
    owner.observe(expected_revision=0)
    service = DashboardService(
        store, RuntimeEventStore(store.root / "runtime-events"),
        CommandQueue(store.path_for(requirement_id) / "dashboard" / "commands.json"),
    )
    assert service.requirement(requirement_id)["projection"]["main_agent"]["stale"] is False
    store.touch_meta(requirement_id, title="Owner freshness changed")
    assert service.requirement(requirement_id)["projection"]["main_agent"]["stale"] is True


def test_dashboard_event_projection_rebuild_is_lossless_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    requirement_id = store.create("事件投影", task_provider=None)
    events = RuntimeEventStore(store.root / "runtime-events")
    event = AgentEvent(
        "event-1", "run-1", "codex", "message", {"text": "一次"},
        session_id="session-1",
    )
    first = events.append(event)
    assert events.append(event) == first
    queue_path = store.path_for(requirement_id) / "dashboard" / "commands.json"

    before = DashboardService(store, events, CommandQueue(queue_path)).requirement(
        requirement_id, run_id="run-1",
    )
    after_restart = DashboardService(
        WorkspaceStore(root, execution_root=root),
        RuntimeEventStore(store.root / "runtime-events"),
        CommandQueue(queue_path),
    ).requirement(requirement_id, run_id="run-1")

    assert before["events"] == after_restart["events"]
    assert before["next_cursor"] == after_restart["next_cursor"] == 1
    assert DashboardService(store, events, CommandQueue(queue_path)).requirement(
        requirement_id, run_id="run-1", after=1,
    )["events"] == []


def test_remote_controller_can_create_dedicated_session(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程控制", task_provider=None)
    bind_session(store, "REQ-001", "session-1")

    def factory(name: str, *, event_sink: object) -> FakeRuntime:
        return FakeRuntime(event_sink)

    controller = RemoteController(
        store, "REQ-001", None, run_id="remote-new", runtime_factory=factory,
    )
    assert controller.message("只回复 READY", command_id="cmd-new")["status"] == "completed"
    assert controller.status()["session_id"] == "session-1"
    assert controller.message(
        "继续", command_id="cmd-next", session_id="new-codex-session",
    )["status"] == "completed"
    assert controller.status()["remote_session_id"] == "session-1"


def test_controller_rejects_session_from_another_requirement(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    first = store.create("第一个需求", task_provider=None)
    second = store.create("第二个需求", task_provider=None)
    bind_session(store, second, "session-other")
    with pytest.raises(WorkspaceError, match="不属于当前 Requirement"):
        RemoteController(store, first, "session-other", run_id="cross-requirement")


def test_controller_execution_details_are_redacted_but_event_store_stays_raw(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Execution redaction")
    execution = ExecutionStore(store).create(
        requirement_id, "TASK-1", role="worker", runtime_id="codex", prompt="run",
    )
    execution = ExecutionStore(store).update(
        execution.id, status="running", session_id="session-1",
    )
    events = RuntimeEventStore(store.root / "runtime-events")
    events.append(AgentEvent(
        "secret-event", execution.id, "codex", "message", {"token": "raw-secret"},
        session_id="session-1", task_id="TASK-1", requirement_id=requirement_id,
        execution_id=execution.id,
    ))
    controller = RemoteController(store, requirement_id, None, run_id="remote")
    displayed = controller.execution_details(execution.id)
    assert displayed["payload_view"] == "redacted"
    assert displayed["details"]["events"][0]["payload"]["token"] == "[REDACTED]"
    assert events.replay(execution.id)[0].payload["token"] == "raw-secret"


def test_controller_restart_marks_delivered_command_failed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    requirement_id = store.create("恢复投递", task_provider=None)
    queue = CommandQueue(store.path_for(requirement_id) / "dashboard" / "commands.json")
    queue.enqueue(requirement_id, "new-codex-session", "继续", command_id="delivered")
    queue.update("delivered", "delivered")
    controller = RemoteController(store, requirement_id, None, run_id="restart")
    command = next(item for item in controller.status()["commands"]
                   if item["command_id"] == "delivered")
    assert command["status"] == "failed" and "结果未知" in command["result"]


class BlockingRuntime(FakeRuntime):
    def __init__(self, event_sink: object) -> None:
        super().__init__(event_sink)
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.steered: list[str] = []
        self.interrupted = False

    def wait(
        self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float,
    ) -> AgentRunResult:
        if turn_id == "turn-2":
            return AgentRunResult(0, session.session_id, "队列完成", "", runtime_id="codex")
        self.waiting.set()
        assert self.release.wait(5)
        return AgentRunResult(
            130 if self.interrupted else 0,
            session.session_id,
            "",
            "已取消" if self.interrupted else "",
            runtime_id="codex",
        )

    def steer(
        self, session: RuntimeSessionRef, turn_id: str, text: str,
    ) -> RuntimeOperationResult:
        self.steered.append(text)
        return RuntimeOperationResult("ok", session, turn_id)

    def interrupt(
        self, session: RuntimeSessionRef, turn_id: str,
    ) -> RuntimeOperationResult:
        self.interrupted = True
        self.release.set()
        return RuntimeOperationResult("ok", session, turn_id)


def test_controller_steers_queues_and_cancels_without_terminal_overwrite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程控制", task_provider=None)
    bind_session(store, "REQ-001", "session-1")
    runtime = BlockingRuntime(object())
    controller = RemoteController(
        store,
        "REQ-001",
        "session-1",
        run_id="remote-active",
        runtime_factory=lambda name, event_sink: runtime,
    )
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(controller.message("开始", command_id="cmd-active")),
    )
    worker.start()
    assert runtime.waiting.wait(5)

    steered = controller.message("补充", command_id="cmd-steer", delivery="auto")
    queued = controller.message("下一条", command_id="cmd-queued", delivery="queue")
    assert steered["status"] == "completed" and runtime.steered == ["补充"]
    assert queued["status"] == "queued"
    assert controller.cancel("cmd-queued")["status"] == "cancelled"
    assert controller.cancel("cmd-active")["status"] == "cancelled"
    worker.join(5)
    assert not worker.is_alive() and result["status"] == "cancelled"


def test_controller_drains_persistent_next_turn_after_active_turn(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("队列投递", task_provider=None)
    bind_session(store, "REQ-001", "session-1")
    runtime = BlockingRuntime(object())
    controller = RemoteController(
        store,
        "REQ-001",
        "session-1",
        run_id="remote-drain",
        runtime_factory=lambda name, event_sink: runtime,
    )
    worker = threading.Thread(
        target=lambda: controller.message("第一条", command_id="cmd-first"),
    )
    worker.start()
    assert runtime.waiting.wait(5)
    assert controller.message(
        "下一条", command_id="cmd-next", delivery="queue",
    )["status"] == "queued"
    runtime.release.set()
    worker.join(5)
    assert not worker.is_alive()
    history = {item.command_id: item for item in controller.commands.history()}
    assert history["cmd-first"].status == "completed"
    assert history["cmd-next"].status == "completed"


class RetryRuntime(FakeRuntime):
    attempts = 0

    def wait(
        self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float,
    ) -> AgentRunResult:
        self.attempts += 1
        return AgentRunResult(
            1 if self.attempts == 1 else 0,
            session.session_id,
            "重试成功" if self.attempts > 1 else "",
            "首次失败" if self.attempts == 1 else "",
            runtime_id="codex",
        )


def test_controller_failed_retry_is_explicit_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程控制", task_provider=None)
    bind_session(store, "REQ-001", "session-1")
    runtime = RetryRuntime(object())
    controller = RemoteController(
        store,
        "REQ-001",
        "session-1",
        run_id="remote-retry",
        runtime_factory=lambda name, event_sink: runtime,
    )
    assert controller.message("执行", command_id="cmd-fail")["status"] == "failed"
    first = controller.retry("cmd-fail", retry_id="cmd-retry")
    second = controller.retry("cmd-fail", retry_id="cmd-retry")
    assert first == second
    assert first["status"] == "completed" and first["retry_of"] == "cmd-fail"
    assert runtime.attempts == 2


@contextmanager
def dashboard_server(controller: RemoteController, token: str):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop, ready = threading.Event(), threading.Event()
    thread = threading.Thread(
        target=serve_remote_control,
        kwargs={
            "controller": controller,
            "token": token,
            "port": port,
            "stop_event": stop,
            "ready_event": ready,
        },
    )
    thread.start()
    assert ready.wait(5)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        thread.join(5)
        assert not thread.is_alive()


def request_json(
    url: str,
    token: str,
    *,
    payload: dict[str, object] | None = None,
    origin: str | None = None,
) -> tuple[int, dict[str, object]]:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if origin is not None:
        headers["Origin"] = origin
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())
    return response.status, json.loads(response.read())


def test_dashboard_http_auth_origin_cursor_redaction_and_requirement_isolation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程控制", task_provider=None)
    store.write_text(store.path_for("REQ-001") / "state.md", "# 状态\n\ntoken=never-return-this")
    original_meta = store.load("REQ-001")["meta"]
    controller = RemoteController(
        store,
        "REQ-001",
        None,
        run_id="remote-http",
        runtime_factory=lambda name, event_sink: FakeRuntime(event_sink),
    )
    token = "t" * 48
    with dashboard_server(controller, token) as base:
        status, body = request_json(f"{base}/api/status?after=0&limit=20", token)
        assert status == 200 and body["requirement_id"] == "REQ-001"
        assert "never-return-this" not in json.dumps(body)
        status, _ = request_json(
            f"{base}/api/status", token, origin="https://attacker.invalid",
        )
        assert status == 403
        status, _ = request_json(f"{base}/api/status?after=-1", token)
        assert status == 400
        status, _ = request_json(
            f"{base}/api/message",
            token,
            payload={
                "message": "越界",
                "command_id": "cross-req",
                "session_id": "another-requirement-session",
            },
        )
        assert status == 400
        status, queued = request_json(
            f"{base}/api/message",
            token,
            payload={
                "message": "稍后执行",
                "command_id": "http-queued",
                "delivery": "queue",
            },
        )
        assert status == 202 and queued["status"] == "queued"
        status, cancelled = request_json(
            f"{base}/api/commands/http-queued/cancel", token, payload={},
        )
        assert status == 200 and cancelled["status"] == "cancelled"
    assert store.load("REQ-001")["meta"] == original_meta
    assert RuntimeEventStore(store.root / "runtime-events").replay("remote-http") == ()


def test_dashboard_http_execution_details_are_bounded_and_redacted(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Execution HTTP")
    execution = ExecutionStore(store).create(
        requirement_id, "TASK-1", role="worker", runtime_id="codex", prompt="run",
    )
    execution = ExecutionStore(store).update(
        execution.id, status="running", session_id="session-1",
    )
    events = RuntimeEventStore(store.root / "runtime-events")
    for index in range(2):
        events.append(AgentEvent(
            f"http-event-{index}", execution.id, "codex", "message",
            {"token": f"secret-{index}"}, session_id="session-1", task_id="TASK-1",
            requirement_id=requirement_id, execution_id=execution.id,
        ))
    controller = RemoteController(store, requirement_id, None, run_id="remote-http-execution")
    token = "e" * 48
    with dashboard_server(controller, token) as base:
        code, page = request_json(f"{base}/api/executions/{execution.id}?limit=1", token)
        assert code == 200 and page["payload_view"] == "redacted"
        assert page["has_more"] is True and page["next_cursor"] == 1
        event_rows = page["details"]["events"]
        assert isinstance(event_rows, list) and event_rows[0]["payload"]["token"] == "[REDACTED]"
        code, error = request_json(f"{base}/api/executions/{execution.id}?limit=201", token)
        assert code == 400 and "limit" in str(error["error"])
def test_dashboard_http_failed_retry_endpoint_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    store.create("远程重试", task_provider=None)
    runtime = RetryRuntime(object())
    controller = RemoteController(
        store,
        "REQ-001",
        None,
        run_id="remote-http-retry",
        runtime_factory=lambda name, event_sink: runtime,
    )
    token = "r" * 48
    with dashboard_server(controller, token) as base:
        status, failed = request_json(
            f"{base}/api/message",
            token,
            payload={"message": "执行", "command_id": "http-failed"},
        )
        assert status == 502 and failed["status"] == "failed"
        payload = {"retry_id": "http-retry-1"}
        status, first = request_json(
            f"{base}/api/commands/http-failed/retry", token, payload=payload,
        )
        status2, second = request_json(
            f"{base}/api/commands/http-failed/retry", token, payload=payload,
        )
        assert status == status2 == 200 and first == second
        assert first["status"] == "completed" and runtime.attempts == 2


class Main:
    def __init__(
        self,
        sha: str,
        branch: str | None = "main",
        *,
        remote_sha: str | None = None,
        clean: bool = True,
        head: str | None = None,
    ) -> None:
        self.sha, self.branch = sha, branch
        self.remote_sha = sha if remote_sha is None else remote_sha
        self.clean = clean
        self.head = head or sha

    def state(self) -> tuple[str | None, str | None, bool, str]:
        return self.branch, self.remote_sha, self.clean, self.head


class Provider:
    calls = 0

    def deploy(
        self, *, environment: str, commit_sha: str, timeout_seconds: float,
    ) -> tuple[bool, str, str]:
        self.calls += 1
        return True, "ok", "rollback"


class Authority:
    def __init__(self, auth: DeploymentAuthorization, merge: MergeReceipt,
                 verification: str = "post") -> None:
        self.value = DeploymentAuthority(auth, merge, verification)

    def resolve(self, authorization_id: str) -> DeploymentAuthority:
        if authorization_id != self.value.authorization.authorization_id:
            raise DeploymentError("authority_missing", "部署授权不存在")
        return self.value


def test_deployment_is_main_only_and_idempotent(tmp_path: Path) -> None:
    sha, tree = "a" * 40, "b" * 40
    merge = MergeReceipt("merge-1", "REQ-020", "request-1", "merged", "c" * 40,
                         sha, tree, "refs/heads/integration", "integration-auth",
                         "pre", "post", "2026-09-07T00:00:00+00:00")
    auth = DeploymentAuthorization("deploy-auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = Provider()
    authority = Authority(auth, merge)
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider, authority,
    )
    first = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    second = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert first == second and provider.calls == 1
    completion = service.complete(
        "REQ-020", sha, receipt=first,
    )
    assert completion.deployment_receipt_id == first.receipt_id
    assert service.complete(
        "REQ-020", sha, receipt=first,
    ) == completion
    no_deploy = DeploymentService(
        tmp_path / "no-deploy",
        DeploymentPolicy("prod", "1", deployment_required=False),
        Main(sha), provider, authority,
    )
    skipped = no_deploy.complete("REQ-020", sha)
    assert skipped.deployment_receipt_id is None
    assert no_deploy.complete("REQ-020", sha) == skipped

    blocked = DeploymentService(tmp_path / "blocked", DeploymentPolicy("prod", "1"),
                                Main(sha, "feature"), provider, authority)
    with pytest.raises(DeploymentError, match="main"):
        blocked.deploy(auth, merge, post_merge_verification_receipt_id="post")


def test_deployment_rejects_forged_authority_before_provider(tmp_path: Path) -> None:
    sha = "a" * 40
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40, sha, "b" * 40,
        "refs/heads/integration", "integration-auth", "pre", "post",
        "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization("auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = Provider()
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider,
        Authority(auth, merge),
    )
    forged = DeploymentAuthorization(
        "auth", "REQ-OTHER", "prod", sha, "merge-1", "post",
    )
    with pytest.raises(DeploymentError) as caught:
        service.deploy(forged, merge, post_merge_verification_receipt_id="post")
    assert caught.value.code == "authority_mismatch" and provider.calls == 0


def test_same_deployment_identity_cannot_repeat_across_requirements(tmp_path: Path) -> None:
    sha = "a" * 40
    provider = Provider()
    first_merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40, sha, "b" * 40,
        "refs/heads/integration", "integration-auth", "pre", "post",
        "2026-09-07T00:00:00+00:00",
    )
    first_auth = DeploymentAuthorization(
        "auth-1", "REQ-020", "prod", sha, "merge-1", "post",
    )
    first = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider,
        Authority(first_auth, first_merge),
    )
    first.deploy(first_auth, first_merge, post_merge_verification_receipt_id="post")
    second_merge = MergeReceipt(
        "merge-2", "REQ-021", "request-2", "merged", "c" * 40, sha, "b" * 40,
        "refs/heads/integration", "integration-auth", "pre", "post",
        "2026-09-07T00:00:00+00:00",
    )
    second_auth = DeploymentAuthorization(
        "auth-2", "REQ-021", "prod", sha, "merge-2", "post",
    )
    second = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider,
        Authority(second_auth, second_merge),
    )
    with pytest.raises(DeploymentError) as caught:
        second.deploy(second_auth, second_merge, post_merge_verification_receipt_id="post")
    assert caught.value.code == "receipt_mismatch" and provider.calls == 1


def test_tampered_existing_deployment_receipt_fails_closed(tmp_path: Path) -> None:
    sha = "a" * 40
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40, sha, "b" * 40,
        "refs/heads/integration", "integration-auth", "pre", "post",
        "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization("auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = Provider()
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider,
        Authority(auth, merge),
    )
    service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    receipt_path = tmp_path / f"project-prod-{sha}-1.json"
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["provider_id"] = "forged"
    receipt_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DeploymentError) as caught:
        service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert caught.value.code == "receipt_tampered" and provider.calls == 1


def test_failed_receipt_cannot_be_tampered_into_success(tmp_path: Path) -> None:
    sha = "a" * 40
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40, sha, "b" * 40,
        "refs/heads/integration", "integration-auth", "pre", "post",
        "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization("auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = FailedProvider()
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider,
        Authority(auth, merge),
    )
    failed = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert failed.status == "failed"
    receipt_path = tmp_path / f"project-prod-{sha}-1.json"
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["status"] = "succeeded"
    receipt_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DeploymentError) as caught:
        service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert caught.value.code == "receipt_tampered"
    forged = DeploymentReceipt(**document)
    with pytest.raises(DeploymentError) as completion:
        service.complete("REQ-020", sha, receipt=forged)
    assert completion.value.code == "receipt_untrusted" and provider.calls == 1


@pytest.mark.parametrize(
    ("main", "code"),
    [
        (Main("a" * 40, "feature"), "not_protected_main"),
        (Main("a" * 40, "integration"), "not_protected_main"),
        (Main("a" * 40, None), "not_protected_main"),
        (Main("a" * 40, clean=False), "not_protected_main"),
        (Main("a" * 40, head="b" * 40), "not_protected_main"),
        (Main("a" * 40, remote_sha="b" * 40), "remote_main_drift"),
    ],
)
def test_deployment_rejects_every_non_current_main_state(
    tmp_path: Path, main: Main, code: str,
) -> None:
    sha = "a" * 40
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40,
        sha, "b" * 40, "refs/heads/integration", "integration-auth",
        "pre", "post", "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization("auth", "REQ-020", "prod", sha, "merge-1", "post")
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), main, Provider(), Authority(auth, merge),
    )
    with pytest.raises(DeploymentError) as caught:
        service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert caught.value.code == code


@pytest.mark.parametrize(
    "mutation",
    ["missing_merge", "stale_merge", "wrong_environment", "stale_verification"],
)
def test_deployment_rejects_missing_or_stale_authority(
    tmp_path: Path, mutation: str,
) -> None:
    sha = "a" * 40
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1",
        "recovery_required" if mutation == "missing_merge" else "merged",
        "c" * 40,
        "d" * 40 if mutation == "stale_merge" else sha,
        "b" * 40, "refs/heads/integration", "integration-auth",
        "pre", "post", "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization(
        "auth", "REQ-020", "other" if mutation == "wrong_environment" else "prod",
        sha, "merge-1", "old" if mutation == "stale_verification" else "post",
    )
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), Provider(), Authority(auth, merge),
    )
    with pytest.raises(DeploymentError):
        service.deploy(auth, merge, post_merge_verification_receipt_id="post")


class DriftingMain(Main):
    calls = 0

    def state(self) -> tuple[str | None, str | None, bool, str]:
        self.calls += 1
        if self.calls == 1:
            return super().state()
        return "main", "b" * 40, True, "b" * 40


def test_deployment_rechecks_main_immediately_before_provider(tmp_path: Path) -> None:
    sha = "a" * 40
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40,
        sha, "b" * 40, "refs/heads/integration", "integration-auth",
        "pre", "post", "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization("auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = Provider()
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), DriftingMain(sha), provider,
        Authority(auth, merge),
    )
    with pytest.raises(DeploymentError, match="main"):
        service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert provider.calls == 0


class FailedProvider(Provider):
    def deploy(
        self, *, environment: str, commit_sha: str, timeout_seconds: float,
    ) -> tuple[bool, str, str]:
        self.calls += 1
        return False, "部署失败", "执行 rollback-1"


class OfflineProvider(Provider):
    def deploy(
        self, *, environment: str, commit_sha: str, timeout_seconds: float,
    ) -> tuple[bool, str, str]:
        self.calls += 1
        raise OSError("offline")


class TimeoutProvider(Provider):
    def deploy(
        self, *, environment: str, commit_sha: str, timeout_seconds: float,
    ) -> tuple[bool, str, str]:
        self.calls += 1
        raise TimeoutError("timeout")


@pytest.mark.parametrize("provider", [FailedProvider(), OfflineProvider(), TimeoutProvider()])
def test_deployment_failure_timeout_and_offline_are_persisted_idempotently(
    tmp_path: Path, provider: Provider,
) -> None:
    sha = "a" * 40
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40,
        sha, "b" * 40, "refs/heads/integration", "integration-auth",
        "pre", "post", "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization("auth", "REQ-020", "prod", sha, "merge-1", "post")
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider, Authority(auth, merge),
    )
    first = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert first.status == "failed" and first.completed_at and first.main_proof
    assert service.deploy(auth, merge, post_merge_verification_receipt_id="post") == first
    assert provider.calls == 1


def test_deployment_uncertain_journal_fails_closed_without_duplicate_side_effect(
    tmp_path: Path,
) -> None:
    sha = "a" * 40
    path = tmp_path / f"project-prod-{sha}-1.json"
    path.write_text(
        json.dumps({"status": "in_progress", "commit_sha": sha}), encoding="utf-8",
    )
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40,
        sha, "b" * 40, "refs/heads/integration", "integration-auth",
        "pre", "post", "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization("auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = Provider()
    service = DeploymentService(
        tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider, Authority(auth, merge),
    )
    with pytest.raises(DeploymentError) as caught:
        service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert caught.value.code == "deployment_uncertain" and provider.calls == 0


def test_environment_registry_and_real_local_dry_run(tmp_path: Path) -> None:
    sha = "a" * 40
    registry = DeploymentEnvironmentRegistry(tmp_path / "environments.json")
    environment = registry.register(DeploymentEnvironment("preview", "dry-run", "1"))
    assert registry.register(environment) == environment
    merge = MergeReceipt(
        "merge-1", "REQ-020", "request-1", "merged", "c" * 40,
        sha, "b" * 40, "refs/heads/integration", "integration-auth",
        "pre", "post", "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization(
        "auth", "REQ-020", "preview", sha, "merge-1", "post", "tester",
    )
    service = DeploymentService.from_registry(
        tmp_path / "receipts",
        project_id="ai-dev-os",
        environment="preview",
        registry=registry,
        main=Main(sha),
        providers={"dry-run": DryRunDeploymentProvider()},
        authority=Authority(auth, merge),
    )
    receipt = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert receipt.status == "succeeded"
    assert receipt.project_id == "ai-dev-os" and receipt.provider_id == "dry-run"
    assert receipt.requested_by == "tester" and "dry-run verified" in receipt.detail


def test_local_main_adapter_and_dry_run_use_real_git_facts(tmp_path: Path) -> None:
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True,
    )
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(
            ["git", "-C", str(repo), "config", key, value], check=True,
            capture_output=True,
        )
    (repo / "README.md").write_bytes(b"ready\n")
    for command in (
        ["git", "-C", str(repo), "add", "README.md"],
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        ["git", "-C", str(repo), "push", "-u", "origin", "main"],
    ):
        subprocess.run(command, check=True, capture_output=True)
    main = LocalGitMainStateProvider(repo)
    branch, remote_sha, clean, head = main.state()
    assert branch == "main" and remote_sha == head and clean

    registry = DeploymentEnvironmentRegistry(tmp_path / "environments.json")
    registry.register(DeploymentEnvironment("dry", "dry-run", "1"))
    merge = MergeReceipt(
        "merge-real", "REQ-020", "request-real", "merged", "c" * 40,
        head, "b" * 40, "refs/heads/integration", "integration-auth",
        "pre", "post", "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization(
        "auth-real", "REQ-020", "dry", head, "merge-real", "post", "local-e2e",
    )
    authority_store = DeploymentAuthorityStore(tmp_path / "deployment-authority")
    authority_store.record(DeploymentAuthority(auth, merge, "post"))
    service = DeploymentService.from_registry(
        tmp_path / "deployment-receipts",
        project_id="real-git",
        environment="dry",
        registry=registry,
        main=main,
        providers={"dry-run": DryRunDeploymentProvider()},
        authority=authority_store,
    )
    assert service.deploy(
        auth, merge, post_merge_verification_receipt_id="post",
    ).status == "succeeded"


def test_phase6_completion_token_is_the_only_v2_completion_bridge(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    requirement_id = store.create("完整交付", task_provider=None)
    mark_v2_delivery(store, requirement_id)
    with pytest.raises(WorkspaceError, match="CompletionToken"):
        require_delivery_completion(store, requirement_id)
    sha = "a" * 40
    token = CompletionToken(
        "completion-1", requirement_id, sha, None, "2026-09-07T00:00:00+00:00",
        False, "prod",
    )
    store.write_json(
        store.path_for(requirement_id) / "phase-gates" / "phase-6.json",
        {"status": "PASS", "commit_sha": sha},
    )
    source_root = tmp_path / "deployment-records"
    registry = DeploymentEnvironmentRegistry(tmp_path / "completion-environments.json")
    registry.register(DeploymentEnvironment(
        "prod", "provider", "v1", deployment_required=False,
    ))
    with pytest.raises(DeploymentError, match="部署服务完成记录"):
        publish_completion_token(store, token, source_root=source_root, registry=registry)
    service = DeploymentService(
        source_root, DeploymentPolicy("prod", "v1", deployment_required=False),
        Main(sha), Provider(), Authority(
            DeploymentAuthorization("unused", requirement_id, "prod", sha, "merge", "post"),
            MergeReceipt(
                "merge", requirement_id, "request", "merged", "c" * 40, sha, "b" * 40,
                "refs/heads/integration", "integration-auth", "pre", "post",
                "2026-09-07T00:00:00+00:00",
            ),
        ),
    )
    token = service.complete(requirement_id, sha)
    required_registry = DeploymentEnvironmentRegistry(tmp_path / "required-environments.json")
    required_registry.register(DeploymentEnvironment("prod", "provider", "v1"))
    with pytest.raises(DeploymentError, match="部署策略"):
        publish_completion_token(
            store, token, source_root=source_root, registry=required_registry,
        )
    assert publish_completion_token(
        store, token, source_root=source_root, registry=registry,
    ).is_file()
    require_delivery_completion(store, requirement_id)

    stale = CompletionToken(
        "completion-2", requirement_id, "b" * 40, None,
        "2026-09-07T00:00:00+00:00", False, "prod",
    )
    with pytest.raises(DeploymentError, match="Phase 6"):
        stale_service = DeploymentService(
            tmp_path / "stale-records",
            DeploymentPolicy("prod", "v1", deployment_required=False),
            Main("b" * 40), Provider(), Authority(
                DeploymentAuthorization(
                    "unused", requirement_id, "prod", "b" * 40, "merge", "post",
                ),
                MergeReceipt(
                    "merge", requirement_id, "request", "merged", "c" * 40, "b" * 40,
                    "d" * 40, "refs/heads/integration", "integration-auth", "pre", "post",
                    "2026-09-07T00:00:00+00:00",
                ),
            ),
        )
        stale = stale_service.complete(requirement_id, "b" * 40)
        publish_completion_token(
            store, stale, source_root=tmp_path / "stale-records", registry=registry,
        )


def test_completion_token_accepts_squash_main_only_for_identical_gated_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(
            ["git", "-C", str(root), "config", key, value], check=True, capture_output=True,
        )
    (root / "app.txt").write_text("same tree\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "app.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "candidate"], check=True)
    gate_sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-m", "squash identity"],
        check=True,
    )
    main_sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()
    store = WorkspaceStore(root, execution_root=root)
    requirement_id = store.create("Squash main", task_provider=None)
    store.write_json(
        store.path_for(requirement_id) / "phase-gates" / "phase-6.json",
        {"status": "PASS", "commit_sha": gate_sha},
    )
    source_root = tmp_path / "completion-records"
    service = DeploymentService(
        source_root, DeploymentPolicy("prod", "v1", deployment_required=False),
        Main(main_sha), Provider(), Authority(
            DeploymentAuthorization("unused", requirement_id, "prod", main_sha, "merge", "post"),
            MergeReceipt(
                "merge", requirement_id, "request", "merged", "c" * 40, main_sha,
                "b" * 40, "refs/heads/integration", "integration-auth", "pre", "post",
                "2026-09-07T00:00:00+00:00",
            ),
        ),
    )
    token = service.complete(requirement_id, main_sha)
    registry = DeploymentEnvironmentRegistry(tmp_path / "environments.json")
    registry.register(DeploymentEnvironment(
        "prod", "provider", "v1", deployment_required=False,
    ))
    assert publish_completion_token(
        store, token, source_root=source_root, registry=registry,
    ).is_file()

    (root / "app.txt").write_text("different tree\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "app.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "different"], check=True)
    different_sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()
    different = service.complete(requirement_id, different_sha)
    with pytest.raises(DeploymentError, match="Phase 6"):
        publish_completion_token(
            store, different, source_root=source_root, registry=registry,
        )


def test_deployment_required_e2e_publishes_completion_only_after_success(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    store = WorkspaceStore(root, execution_root=root)
    requirement_id = store.create("必须部署", task_provider=None)
    mark_v2_delivery(store, requirement_id)
    sha = "a" * 40
    store.write_json(
        store.path_for(requirement_id) / "phase-gates" / "phase-6.json",
        {"status": "PASS", "commit_sha": sha},
    )
    merge = MergeReceipt(
        "merge-required", requirement_id, "request", "merged", "c" * 40, sha,
        "b" * 40, "refs/heads/integration", "integration-auth", "pre", "post",
        "2026-09-07T00:00:00+00:00",
    )
    auth = DeploymentAuthorization(
        "auth-required", requirement_id, "prod", sha, "merge-required", "post",
    )
    receipts = tmp_path / "required-receipts"
    service = DeploymentService(
        receipts, DeploymentPolicy("prod", "v1"), Main(sha), Provider(),
        Authority(auth, merge),
    )
    with pytest.raises(DeploymentError, match="成功部署收据"):
        service.complete(requirement_id, sha)
    receipt = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    token = service.complete(requirement_id, sha, receipt=receipt)
    registry = DeploymentEnvironmentRegistry(tmp_path / "required-registry.json")
    registry.register(DeploymentEnvironment("prod", "default", "v1"))
    publish_completion_token(store, token, source_root=receipts, registry=registry)
    require_delivery_completion(store, requirement_id)
