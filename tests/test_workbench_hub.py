from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workspace_orchestrator import workbench_hub
from workspace_orchestrator.project_config import ProjectConfig
from workspace_orchestrator.project_registry import GlobalProjectRegistry
from workspace_orchestrator.workbench_hub import WorkbenchHub, serve_workbench
from workspace_orchestrator.workbench_hub_ui import WORKBENCH_HUB_HTML
from workspace_orchestrator.workspace import WorkspaceStore


def _hub(tmp_path: Path) -> tuple[WorkbenchHub, str, Path]:
    project = tmp_path / "project"
    project.mkdir()
    store = WorkspaceStore(project)
    requirement_id = store.create(
        "核心工作台",
        goal="打开后直接看到需求空间",
        acceptance=["可见", "可进入"],
        task_provider=None,
    )
    registry = GlobalProjectRegistry(tmp_path / "projects.json")
    registered = registry.register(
        project,
        ProjectConfig(None, None, project_id="project-demo"),
    )
    return WorkbenchHub(registry), requirement_id, Path(registered.path)


def test_catalog_lists_registered_projects_and_requirement_spaces(tmp_path: Path) -> None:
    hub, requirement_id, project = _hub(tmp_path)

    catalog = hub.catalog()

    assert catalog["counts"] == {"projects": 1, "requirements": 1, "active": 1}
    listed = catalog["projects"][0]
    assert listed["id"] == "project-demo" and listed["path"] == str(project)
    assert listed["requirements"][0]["id"] == requirement_id
    assert listed["requirements"][0]["title"] == "核心工作台"


def test_hub_creates_requirement_and_opens_full_space(tmp_path: Path) -> None:
    hub, _, _ = _hub(tmp_path)

    created = hub.create_requirement(
        "project-demo",
        {
            "title": "从 Workbench 创建",
            "goal": "不使用内部 CLI",
            "acceptance": ["创建成功", "可立即进入"],
            "complexity": "normal",
        },
    )
    requirement_id = created["requirement_id"]
    detail = hub.requirement("project-demo", requirement_id)

    assert requirement_id == "REQ-002"
    assert detail["workspace"]["meta"]["title"] == "从 Workbench 创建"
    assert "创建成功" in detail["workspace"]["requirement"]
    assert hub.catalog()["counts"]["requirements"] == 2


def test_requirement_detail_redacts_sensitive_workspace_text(tmp_path: Path) -> None:
    hub, requirement_id, project = _hub(tmp_path)
    WorkspaceStore(project).write_text(
        WorkspaceStore(project).path_for(requirement_id) / "state.md",
        "# 状态\n\nsecret=never-return-this",
    )

    detail = hub.requirement("project-demo", requirement_id)

    assert "never-return-this" not in json.dumps(detail, default=str)


def test_start_action_uses_real_workspace_write_runtime_boundary(
    tmp_path: Path, monkeypatch,
) -> None:
    hub, requirement_id, _ = _hub(tmp_path)
    captured: dict[str, object] = {}

    class Service:
        def start(self, request):
            captured["sandbox"] = request.sandbox
            captured["message"] = request.message
            captured["task_id"] = request.task_id
            return SimpleNamespace(
                status="running",
                id="EXE-000001",
                session_id="session-1",
                turn_id="turn-1",
                to_dict=lambda: {"status": "running"},
            )

        def close(self) -> None:
            return

    monkeypatch.setattr(workbench_hub, "configured_workbench", lambda store: Service())

    result = hub.message(
        "project-demo", requirement_id,
        {"message": "实现需求", "command_id": "run-one"},
    )

    assert result["status"] == "running" and result["execution_id"] == "EXE-000001"
    assert captured == {
        "sandbox": "workspace-write",
        "message": "实现需求",
        "task_id": "TASK-WORKBENCH",
    }


@contextmanager
def _server(hub: WorkbenchHub, token: str = "", *, require_token: bool = False):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop, ready = threading.Event(), threading.Event()
    thread = threading.Thread(
        target=serve_workbench,
        kwargs={
            "hub": hub,
            "token": token,
            "port": port,
            "open_browser": False,
            "stop_event": stop,
            "ready_event": ready,
            "require_token": require_token,
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


def _json(url: str, token: str | None, payload: dict[str, object] | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())
    return response.status, json.loads(response.read())


def test_workbench_http_remote_mode_protects_space_api(tmp_path: Path) -> None:
    hub, requirement_id, _ = _hub(tmp_path)
    token = "w" * 48
    with _server(hub, token, require_token=True) as base:
        shell = urlopen(f"{base}/", timeout=5).read().decode()
        unauthorized, _ = _json(f"{base}/api/workbench", None)
        status, catalog = _json(f"{base}/api/workbench", token)
        detail_status, detail = _json(
            f"{base}/api/projects/project-demo/requirements/{requirement_id}", token,
        )

    assert "AI Dev OS Workbench" in shell
    assert unauthorized == 401
    assert status == 200 and catalog["counts"]["requirements"] == 1
    assert detail_status == 200 and detail["requirement_id"] == requirement_id


def test_workbench_http_loopback_mode_needs_no_token(tmp_path: Path) -> None:
    hub, _, _ = _hub(tmp_path)
    with _server(hub) as base:
        status, catalog = _json(f"{base}/api/workbench", None)

    assert status == 200
    assert catalog["counts"]["requirements"] == 1


def test_workbench_ui_exposes_core_requirement_space_actions() -> None:
    for label in (
        "＋ 新需求",
        "开始 / 继续执行",
        "Intent",
        "Acceptance",
        "Task / Execution / Verification",
    ):
        assert label in WORKBENCH_HUB_HTML
    assert "#token=" not in WORKBENCH_HUB_HTML
    assert "REQ-020" not in WORKBENCH_HUB_HTML
