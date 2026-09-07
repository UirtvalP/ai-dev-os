"""以 Requirement Space 为核心的本地 Workbench 首页与控制入口。"""

from __future__ import annotations

import base64
import hmac
import json
import re
import threading
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from .agent_runtime.events import RuntimeEventStore
from .composition import configured_workbench
from .dashboard import CommandQueue, DashboardService
from .models import WorkflowComplexity
from .project_config import load_project_config
from .project_init import register_project
from .project_registry import GlobalProjectRegistry, RegisteredProject
from .remote_control import MAX_BODY_BYTES, MIN_TOKEN_BYTES, _redact
from .workbench import WorkbenchExecutionService, WorkbenchStart
from .workbench_hub_ui import WORKBENCH_HUB_HTML
from .workspace import WorkspaceError, WorkspaceStore, markdown_sections


class WorkbenchHub:
    """从 Global Project Registry 和各项目 Workspace 组装唯一事实视图。"""

    def __init__(self, registry: GlobalProjectRegistry | None = None) -> None:
        self.registry = registry or GlobalProjectRegistry()
        self._workbenches: dict[tuple[str, str], WorkbenchExecutionService] = {}
        self._lock = threading.RLock()

    def catalog(self) -> dict[str, Any]:
        projects = [self._project_summary(project) for project in self.registry.list()]
        return {
            "schema_version": 1,
            "projects": projects,
            "counts": {
                "projects": len(projects),
                "requirements": sum(len(item["requirements"]) for item in projects),
                "active": sum(
                    1 for item in projects for requirement in item["requirements"]
                    if requirement["status"] not in {"done"}
                ),
            },
        }

    def requirement(self, project_id: str, requirement_id: str) -> dict[str, Any]:
        project, store = self._store(project_id)
        requirement_id = requirement_id.upper()
        store.load(requirement_id)
        service = DashboardService(
            store,
            RuntimeEventStore(store.root / "runtime-events"),
            CommandQueue(store.path_for(requirement_id) / "dashboard" / "commands.json"),
        )
        result = service.requirement(requirement_id, run_id=None)
        result.update(
            project_id=project.id,
            project_name=project.name,
            connected=(project.id, requirement_id) in self._workbenches,
        )
        redacted = _redact(result)
        assert isinstance(redacted, dict)
        return redacted

    def create_requirement(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project, store = self._store(project_id)
        title = _required_text(payload, "title", maximum=200)
        goal = _optional_text(payload.get("goal"), maximum=4_000) or title
        acceptance = payload.get("acceptance") or []
        if not isinstance(acceptance, list) or any(
            not isinstance(item, str) or not item.strip() for item in acceptance
        ):
            raise WorkspaceError("acceptance 必须是非空字符串数组")
        complexity_value = payload.get("complexity", "normal")
        try:
            complexity = WorkflowComplexity(str(complexity_value))
        except ValueError as exc:
            raise WorkspaceError("complexity 必须是 tiny、normal、complex 或 research") from exc
        requirement_id = store.create(
            title,
            goal=goal,
            acceptance=[item.strip() for item in acceptance],
            complexity=complexity,
        )
        return {
            "project_id": project.id,
            "requirement_id": requirement_id,
            "requirement": self.requirement(project.id, requirement_id),
        }

    def add_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建或接入一个项目目录，并绑定到全局 Workbench。"""

        raw_path = _required_text(payload, "path", maximum=4_096)
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise WorkspaceError("项目路径必须是绝对路径")
        path = path.resolve()
        mode = _optional_text(payload.get("mode"), maximum=16) or "add"
        if mode not in {"add", "create"}:
            raise WorkspaceError("mode 必须是 add 或 create")
        if mode == "create":
            if path.exists():
                raise WorkspaceError(f"新项目目录已存在：{path}")
            path.mkdir(parents=True)
        elif not path.is_dir():
            raise WorkspaceError(f"要添加的项目目录不存在：{path}")

        result = register_project(path)
        config = load_project_config(result.root)
        if config is None:
            raise WorkspaceError(f"项目初始化后仍缺少配置：{result.root}")
        project = self.registry.register(result.root, config)
        return {
            "project": self._project_summary(project),
            "created": result.created,
            "updated": result.updated,
            "preserved": result.preserved,
        }

    def remove_project(self, project_id: str) -> dict[str, Any]:
        """只解除 Workbench 绑定，不删除项目或 Requirement 文件。"""

        project = self.registry.unregister(unquote(project_id))
        return {
            "project_id": project.id,
            "status": "unregistered",
            "preserved": ["项目目录", ".workspace", "Git", "Task"],
        }

    def message(
        self, project_id: str, requirement_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        project, store = self._store(project_id)
        requirement_id = requirement_id.upper()
        store.load(requirement_id)
        message = _required_text(payload, "message", maximum=8_192)
        command_id = _optional_text(payload.get("command_id"), maximum=128) or uuid4().hex
        execution_id = _optional_text(payload.get("execution_id"), maximum=64)
        key = (project.id, requirement_id)
        with self._lock:
            service = self._workbenches.get(key)
            if service is None:
                service = configured_workbench(store)
                self._workbenches[key] = service
        if execution_id:
            operation = service.reply(
                requirement_id, execution_id.upper(), message, command_id=command_id,
            )
            result: dict[str, Any] = {
                "status": operation.status,
                "execution_id": execution_id.upper(),
                "session_id": operation.session.session_id if operation.session else None,
                "turn_id": operation.turn_id,
                "command_id": command_id,
                "result": operation.data,
            }
            if operation.error:
                result["error"] = operation.error.to_dict()
            return result
        execution = service.start(WorkbenchStart(
            requirement_id=requirement_id,
            task_id="TASK-WORKBENCH",
            message=message,
            runtime_id="codex",
            sandbox="workspace-write",
            creation_key=f"workbench:{command_id}",
        ))
        return {
            "status": execution.status,
            "execution_id": execution.id,
            "session_id": execution.session_id,
            "turn_id": execution.turn_id,
            "command_id": command_id,
            "result": execution.to_dict(),
        }

    def close(self) -> None:
        with self._lock:
            workbenches = tuple(self._workbenches.values())
            self._workbenches.clear()
        failures: list[str] = []
        for workbench in workbenches:
            try:
                workbench.close()
            except Exception as exc:  # noqa: BLE001 -- 关闭其余 Runtime 后再统一报告。
                failures.append(str(exc))
        if failures:
            raise WorkspaceError("Workbench Runtime 清理失败：" + "；".join(failures))

    def _project_summary(self, project: RegisteredProject) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": project.id,
            "name": project.name,
            "path": project.path,
            "status": project.status,
            "requirements": [],
        }
        if project.status != "active":
            return result
        try:
            store = WorkspaceStore(Path(project.path))
            result["requirements"] = [
                self._requirement_summary(store, requirement_id)
                for requirement_id in reversed(store.requirement_ids())
            ]
        except (OSError, WorkspaceError) as exc:
            result["status"] = "invalid"
            result["error"] = str(exc)
        return result

    @staticmethod
    def _requirement_summary(store: WorkspaceStore, requirement_id: str) -> dict[str, Any]:
        snapshot = store.load(requirement_id)
        meta = snapshot["meta"]
        state = markdown_sections(snapshot["state"])
        next_action = state.get("Next Action", "")
        return {
            "id": requirement_id,
            "title": meta.get("title") or requirement_id,
            "status": meta.get("status") or "unknown",
            "complexity": meta.get("complexity") or "normal",
            "updated_at": meta.get("updated_at"),
            "phase": state.get("Phase", ""),
            "next_action": next_action,
        }

    def _store(self, project_id: str) -> tuple[RegisteredProject, WorkspaceStore]:
        project = self.registry.show(unquote(project_id))
        if project.status != "active":
            raise WorkspaceError(f"项目不可用：{project.id}")
        return project, WorkspaceStore(Path(project.path))


def serve_workbench(
    hub: WorkbenchHub,
    *,
    token: str = "",
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    stop_event: threading.Event | None = None,
    ready_event: threading.Event | None = None,
    require_token: bool = False,
    username: str = "",
    ready_callback: Callable[[], None] | None = None,
) -> None:
    """启动 Requirement Space；本地默认免密，远程入口使用 Bearer 或账密认证。"""

    loopback = host in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not require_token:
        raise WorkspaceError("局域网或公网监听必须启用 --remote-access")
    if require_token and len(token.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise WorkspaceError("Workbench token 至少需要 32 字节")
    if username and not require_token:
        raise WorkspaceError("账密认证必须同时启用 --remote-access")
    auth_mode = "basic" if username else ("bearer" if require_token else "none-loopback")
    shell = WORKBENCH_HUB_HTML.replace("__AUTH_MODE__", json.dumps(auth_mode))
    basic_credential = base64.b64encode(f"{username}:{token}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AI-Dev-OS-Workbench/0.1"

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                self._bytes(HTTPStatus.OK, shell.encode(), "text/html; charset=utf-8")
                return
            if not self._guard():
                return
            try:
                if path == "/api/workbench":
                    self._json(HTTPStatus.OK, hub.catalog())
                    return
                match = re.fullmatch(
                    r"/api/projects/([^/]+)/requirements/(REQ-\d{3,})", path, re.IGNORECASE,
                )
                if match:
                    self._json(HTTPStatus.OK, hub.requirement(*match.groups()))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
            except (OSError, WorkspaceError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if not self._guard():
                return
            try:
                if path == "/api/projects":
                    payload = self._payload({"path", "mode"})
                    self._json(HTTPStatus.CREATED, hub.add_project(payload))
                    return
                create_match = re.fullmatch(r"/api/projects/([^/]+)/requirements", path)
                if create_match:
                    payload = self._payload({"title", "goal", "acceptance", "complexity"})
                    self._json(
                        HTTPStatus.CREATED,
                        hub.create_requirement(create_match.group(1), payload),
                    )
                    return
                message_match = re.fullmatch(
                    r"/api/projects/([^/]+)/requirements/(REQ-\d{3,})/message",
                    path,
                    re.IGNORECASE,
                )
                if message_match:
                    payload = self._payload({"message", "command_id", "execution_id"})
                    self._json(
                        HTTPStatus.ACCEPTED,
                        hub.message(
                            message_match.group(1), message_match.group(2), payload,
                        ),
                    )
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
            except (json.JSONDecodeError, OSError, WorkspaceError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_DELETE(self) -> None:
            path = urlsplit(self.path).path
            if not self._guard():
                return
            try:
                match = re.fullmatch(r"/api/projects/([^/]+)", path)
                if match:
                    self._json(HTTPStatus.OK, hub.remove_project(match.group(1)))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
            except (OSError, WorkspaceError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _guard(self) -> bool:
            if require_token:
                supplied = self.headers.get("Authorization", "")
                expected = (
                    f"Basic {basic_credential}" if username else f"Bearer {token}"
                )
                if not hmac.compare_digest(supplied.encode(), expected.encode()):
                    message = "用户名或密码错误" if username else "需要有效 Bearer token"
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": message})
                    return False
            origin = self.headers.get("Origin")
            if origin is not None:
                parsed = urlsplit(origin)
                if parsed.scheme not in {"http", "https"} or (
                    parsed.netloc.lower() != self.headers.get("Host", "").lower()
                ):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "请求来源与 Workbench 不一致"})
                    return False
            return True

        def _payload(self, allowed: set[str]) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise WorkspaceError("请求体大小无效")
            if self.headers.get_content_type() != "application/json":
                raise WorkspaceError("Content-Type 必须是 application/json")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not set(payload) <= allowed:
                raise WorkspaceError("请求字段无效")
            return payload

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._bytes(
                status,
                json.dumps(payload, ensure_ascii=False, default=str).encode(),
                "application/json; charset=utf-8",
            )

        def _bytes(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-AI-Dev-OS-Auth", auth_mode)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.timeout = 0.1
    if ready_event is not None:
        ready_event.set()
    if ready_callback is not None:
        ready_callback()
    if open_browser:
        address = "127.0.0.1" if host in {"::1", "localhost"} else host
        suffix = f"#token={quote(token, safe='')}" if require_token and not username else ""
        webbrowser.open(f"http://{address}:{port}/{suffix}")
    try:
        if stop_event is None:
            server.serve_forever(poll_interval=0.25)
        else:
            while not stop_event.is_set():
                server.handle_request()
    finally:
        server.server_close()
        hub.close()


def _required_text(payload: dict[str, Any], name: str, *, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"{name} 不能为空")
    return _optional_text(value, maximum=maximum) or ""


def _optional_text(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkspaceError("文本字段类型无效")
    result = value.strip()
    if len(result) > maximum:
        raise WorkspaceError(f"文本字段超过 {maximum} 字符限制")
    return result or None
