"""Phase 5 的最小远程控制入口：回环监听、Bearer 认证、只读 Codex Runtime。"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .agent_runtime.contracts import AgentRunRequest, RuntimeOperationResult, RuntimeSessionRef
from .agent_runtime.events import RuntimeEventStore
from .agent_runtime.ports import AgentRuntimePort
from .composition import create_runtime
from .dashboard import CommandQueue, DashboardCommand, DashboardService
from .dashboard_ui import DASHBOARD_HTML
from .workspace import WorkspaceError, WorkspaceStore

MAX_BODY_BYTES = 65_536
MIN_TOKEN_BYTES = 32


def load_or_create_token(path: Path) -> str:
    """生成高熵 token；服务日志和页面均不回显。"""

    path = path.expanduser().resolve()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(48)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        temporary.write_text(token + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise WorkspaceError("Dashboard token 至少需要 32 字节")
    return token


class RemoteController:
    """串行桥接固定 Requirement/Session，远端不能改目标或审批。"""

    def __init__(
        self,
        workspace: WorkspaceStore,
        requirement_id: str,
        session_id: str | None,
        *,
        run_id: str,
        runtime_factory: Callable[..., AgentRuntimePort] = create_runtime,
        wait_seconds: float = 300,
    ) -> None:
        snapshot = workspace.load(requirement_id)
        known_sessions = {
            str(item.get("id")) for item in snapshot["sessions"] if item.get("id")
        }
        if session_id is not None and (
            not session_id.strip() or session_id not in known_sessions
        ):
            raise WorkspaceError("Codex Session 不属于当前 Requirement")
        self.workspace = workspace
        self.requirement_id = requirement_id
        self.session_id = session_id
        self._queue_session_id = session_id or "new-codex-session"
        self._dedicated_session_id = session_id
        self.run_id = run_id
        self.wait_seconds = wait_seconds
        self.events = RuntimeEventStore(workspace.root / "runtime-events")
        self.commands = CommandQueue(
            workspace.path_for(requirement_id) / "dashboard" / "commands.json",
            max_message_length=8_192,
        )
        self.dashboard = DashboardService(workspace, self.events, self.commands)
        self._runtime_factory = runtime_factory
        self._runtime: AgentRuntimePort | None = None
        self._session: RuntimeSessionRef | None = None
        self._active_command_id: str | None = None
        self._active_turn_id: str | None = None
        self._lock = threading.RLock()
        self.commands.recover_delivered(requirement_id)

    def status(self, *, after: int = 0, limit: int = 500) -> dict[str, Any]:
        with self._lock:
            thread: dict[str, Any] | None = None
            if self._runtime is not None and self._session is not None:
                result = self._runtime.read_session(self._session)
                thread = result.data.get("thread") if result.ok else {
                    "error": result.error.to_dict() if result.error else "读取失败"
                }
            snapshot = self.dashboard.requirement(
                self.requirement_id, run_id=self.run_id, after=after, limit=limit,
            )
            snapshot.update(
                session_id=self.session_id,
                remote_session_id=self._dedicated_session_id,
                run_id=self.run_id,
                connected=self._session is not None,
                thread=thread,
                agent_detail=_normalize_thread(thread),
                commands=[asdict(item) for item in self.commands.history(
                    requirement_id=self.requirement_id,
                )[-100:]],
                safety={"sandbox": "read-only", "approvals": "deny"},
                active_command_id=self._active_command_id,
            )
            redacted = _redact(snapshot)
            assert isinstance(redacted, dict)
            return redacted

    def message(
        self,
        text: str,
        *,
        command_id: str | None = None,
        session_id: str | None = None,
        delivery: str = "auto",
    ) -> dict[str, Any]:
        message = _validate_message(text)
        if delivery not in {"auto", "steer", "new_turn", "queue"}:
            raise WorkspaceError("delivery 必须是 auto、steer、new_turn 或 queue")
        target = self._validate_target(session_id or self._queue_session_id)
        command = self.commands.enqueue(
            self.requirement_id, target, message, command_id=command_id,
        )
        if command.status != "queued":
            return asdict(command)
        if delivery == "queue":
            return asdict(command)

        with self._lock:
            if self._active_turn_id is not None:
                if delivery in {"auto", "steer"} and target == self._queue_session_id:
                    assert self._runtime is not None and self._session is not None
                    operation = self._runtime.steer(
                        self._session, self._active_turn_id, message,
                    )
                    if operation.ok:
                        self.commands.update(command.command_id, "delivered")
                        return asdict(self.commands.update(
                            command.command_id, "completed", "已注入当前 Turn",
                        ))
                    if delivery == "steer":
                        detail = operation.error.message if operation.error else "Runtime 不支持 steer"
                        return asdict(self.commands.update(command.command_id, "failed", detail))
                return asdict(command)
        result = self._execute(command)
        self._drain_pending()
        return asdict(result)

    def cancel(self, command_id: str) -> dict[str, Any]:
        command = self._find_command(command_id)
        if command.status == "queued":
            return asdict(self.commands.cancel(command_id))
        with self._lock:
            if command_id != self._active_command_id:
                return asdict(command)
            assert self._runtime is not None and self._session is not None
            assert self._active_turn_id is not None
            operation = self._runtime.interrupt(self._session, self._active_turn_id)
            if not operation.ok:
                detail = operation.error.message if operation.error else "Runtime 中断失败"
                raise WorkspaceError(detail)
            return asdict(self.commands.update(command_id, "cancelled", "用户已中断当前 Turn"))

    def retry(self, command_id: str, *, retry_id: str) -> dict[str, Any]:
        command = self.commands.retry(command_id, retry_id=retry_id)
        if command.status != "queued":
            return asdict(command)
        result = self._execute(command)
        self._drain_pending()
        return asdict(result)

    def deliver(self, command_id: str) -> dict[str, Any]:
        command = self._find_command(command_id)
        if command.status != "queued":
            return asdict(command)
        with self._lock:
            if self._active_turn_id is not None:
                return asdict(command)
        result = self._execute(command)
        self._drain_pending()
        return asdict(result)

    def _execute(self, command: DashboardCommand) -> DashboardCommand:
        with self._lock:
            if self._active_turn_id is not None:
                return command
            self._activate_target(command.session_id)
            self.commands.update(command.command_id, "delivered")
            try:
                operation = self._start_or_send(command.message)
            except (WorkspaceError, RuntimeError, OSError, ValueError) as exc:
                return self.commands.update(command.command_id, "failed", str(exc))
            if not operation.ok or operation.session is None or operation.turn_id is None:
                detail = operation.error.message if operation.error else "Runtime 未返回 turn"
                return self.commands.update(command.command_id, "failed", detail)
            self._active_command_id = command.command_id
            self._active_turn_id = operation.turn_id
            runtime, session, turn_id = self._runtime, operation.session, operation.turn_id
        assert runtime is not None
        try:
            result = runtime.wait(session, turn_id, timeout_seconds=self.wait_seconds)
            if result.returncode != 0:
                detail = result.error.message if result.error else result.stderr or "轮次失败"
                return self.commands.update(command.command_id, "failed", detail)
            return self.commands.update(
                command.command_id, "completed", result.summary or result.stdout,
            )
        except (WorkspaceError, RuntimeError, OSError, ValueError) as exc:
            return self.commands.update(command.command_id, "failed", str(exc))
        finally:
            with self._lock:
                if self._active_command_id == command.command_id:
                    self._active_command_id = None
                    self._active_turn_id = None

    def _drain_pending(self) -> None:
        while True:
            with self._lock:
                if self._active_turn_id is not None:
                    return
                pending = self.commands.pending(self._queue_session_id)
            if not pending:
                return
            self._execute(pending[0])

    def _find_command(self, command_id: str) -> DashboardCommand:
        command = next((item for item in self.commands.history(
            requirement_id=self.requirement_id,
        ) if item.command_id == command_id), None)
        if command is None:
            raise WorkspaceError("找不到指令")
        return command

    def _validate_target(self, session_id: str) -> str:
        known = {
            str(session.get("id"))
            for session in self.workspace.load(self.requirement_id)["sessions"]
            if session.get("id")
        }
        known.add("new-codex-session")
        if session_id not in known:
            raise WorkspaceError("目标 Session 不属于当前 Requirement")
        return session_id

    def _activate_target(self, session_id: str) -> None:
        if session_id == self._queue_session_id:
            return
        if self._runtime is not None:
            self._runtime.close()
        self._runtime = None
        self._session = None
        self.session_id = (
            self._dedicated_session_id if session_id == "new-codex-session" else session_id
        )
        self._queue_session_id = session_id

    def _start_or_send(self, message: str) -> RuntimeOperationResult:
        if self._runtime is None:
            self._runtime = self._runtime_factory("codex", event_sink=self.events.append)
            request = AgentRunRequest(
                run_id=self.run_id,
                workspace_path=self.workspace.working_root,
                prompt=message,
                sandbox="read-only",
                resume_session_id=self.session_id,
                requirement_id=self.requirement_id,
            )
            operation = self._runtime.resume(request) if self.session_id else self._runtime.start(request)
            if operation.ok:
                self._session = operation.session
                assert operation.session is not None
                self.session_id = operation.session.session_id
                if self._queue_session_id == "new-codex-session":
                    self._dedicated_session_id = operation.session.session_id
            else:
                self._runtime.close()
                self._runtime = None
            return operation
        assert self._session is not None
        return self._runtime.send_message(self._session, message)

    def close(self) -> None:
        with self._lock:
            if self._runtime is not None:
                self._runtime.close()
                self._runtime = None
                self._session = None


def _validate_message(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError("消息不能为空")
    message = value.strip()
    if len(message) > 8_192:
        raise WorkspaceError("消息超过 8192 字符限制")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in message):
        raise WorkspaceError("消息包含不允许的控制字符")
    return message


_SENSITIVE_KEY = re.compile(
    r"^(?:authorization|password|passwd|secret|token|access[_-]?token|api[_-]?key)$",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:bearer\s+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*)[^\s,;\"']+"
)


def _redact(value: Any, *, key: str = "") -> Any:
    """在 HTTP 边界脱敏；Workspace 与 Event Store 中的原始事实保持不变。"""

    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {name: _redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub("[REDACTED]", value)
    return value


def _normalize_thread(thread: dict[str, Any] | None) -> dict[str, Any]:
    """把 Provider 会话投影成稳定的消息/工具/文件/验证/审批/用量视图。"""

    detail: dict[str, Any] = {
        "messages": [],
        "tools": [],
        "file_changes": [],
        "verification": [],
        "approvals": [],
        "usage": {},
        "current_turn": None,
    }
    if not isinstance(thread, dict):
        return detail
    detail["usage"] = thread.get("tokenUsage") or thread.get("usage") or {}
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return detail
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id, turn_status = turn.get("id"), turn.get("status")
        if turn_status not in {"completed", "failed", "cancelled"}:
            detail["current_turn"] = {"id": turn_id, "status": turn_status or "active"}
        if turn.get("tokenUsage") or turn.get("usage"):
            detail["usage"] = turn.get("tokenUsage") or turn.get("usage")
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = {
                "turn_id": turn_id,
                "type": item.get("type") or "unknown",
                "id": item.get("id"),
                "text": _item_text(item),
                "status": item.get("status"),
            }
            kind = str(normalized["type"]).lower()
            if kind in {"usermessage", "agentmessage", "message"}:
                normalized["role"] = "user" if kind == "usermessage" else "agent"
                detail["messages"].append(normalized)
            elif "file" in kind and ("change" in kind or "edit" in kind):
                normalized["path"] = item.get("path") or item.get("file")
                detail["file_changes"].append(normalized)
            elif "approval" in kind or "request" in kind:
                detail["approvals"].append(normalized)
            elif "verification" in kind or "test" in kind:
                detail["verification"].append(normalized)
            elif "tool" in kind or "command" in kind:
                normalized["name"] = item.get("name") or item.get("command")
                detail["tools"].append(normalized)
            else:
                detail["tools"].append(normalized)
    return detail


def _item_text(item: dict[str, Any]) -> str:
    text = item.get("text") or item.get("output") or item.get("summary")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts = []
        for value in content:
            if isinstance(value, dict):
                piece = value.get("text") or value.get("output")
                if isinstance(piece, str):
                    parts.append(piece)
        return "\n".join(parts)
    return ""


def serve_remote_control(
    controller: RemoteController,
    *,
    token: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    stop_event: threading.Event | None = None,
    ready_event: threading.Event | None = None,
) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise WorkspaceError("Dashboard 仅允许监听本机回环地址，请通过安全隧道发布")
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise WorkspaceError("Dashboard token 至少需要 32 字节")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AI-Dev-OS-Dashboard/0.1"

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path == "/":
                self._bytes(HTTPStatus.OK, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "需要有效 Bearer token"})
                return
            if not self._same_origin():
                self._json(HTTPStatus.FORBIDDEN, {"error": "请求来源与 Dashboard 不一致"})
                return
            if path == "/api/status":
                try:
                    values = parse_qs(query, strict_parsing=True) if query else {}
                    if not set(values) <= {"after", "limit"}:
                        raise WorkspaceError("status 查询只允许 after 和 limit")
                    after = _bounded_integer(values, "after", default=0, minimum=0, maximum=10**9)
                    limit = _bounded_integer(values, "limit", default=500, minimum=1, maximum=500)
                except (WorkspaceError, ValueError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._json(HTTPStatus.OK, controller.status(after=after, limit=limit))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})

        def do_POST(self) -> None:
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "需要有效 Bearer token"})
                return
            if not self._same_origin():
                self._json(HTTPStatus.FORBIDDEN, {"error": "请求来源与 Dashboard 不一致"})
                return
            try:
                if self.path == "/api/message":
                    payload = self._payload(
                        {"message", "command_id", "session_id", "delivery"}, required=True,
                    )
                    message, command_id = payload.get("message"), payload.get("command_id")
                    session_id, delivery = payload.get("session_id"), payload.get("delivery", "auto")
                    if not isinstance(message, str):
                        raise WorkspaceError("message 必须是字符串")
                    if command_id is not None and not isinstance(command_id, str):
                        raise WorkspaceError("command_id 必须是字符串")
                    if session_id is not None and not isinstance(session_id, str):
                        raise WorkspaceError("session_id 必须是字符串")
                    if not isinstance(delivery, str):
                        raise WorkspaceError("delivery 必须是字符串")
                    result = controller.message(
                        message,
                        command_id=command_id,
                        session_id=session_id,
                        delivery=delivery,
                    )
                    self._json(_command_http_status(result), result)
                    return
                matched = re.fullmatch(
                    r"/api/commands/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})/(cancel|retry|deliver)",
                    self.path,
                )
                if matched is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
                    return
                command_id, action = matched.groups()
                if action == "cancel":
                    self._payload(set(), required=False)
                    result = controller.cancel(command_id)
                elif action == "deliver":
                    self._payload(set(), required=False)
                    result = controller.deliver(command_id)
                else:
                    payload = self._payload({"retry_id"}, required=True)
                    retry_id = payload.get("retry_id")
                    if not isinstance(retry_id, str):
                        raise WorkspaceError("retry_id 必须是字符串")
                    result = controller.retry(command_id, retry_id=retry_id)
                self._json(_command_http_status(result), result)
            except (WorkspaceError, ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _payload(self, allowed: set[str], *, required: bool) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0 and not required:
                return {}
            if length <= 0 or length > MAX_BODY_BYTES:
                raise WorkspaceError("请求体大小无效")
            if self.headers.get_content_type() != "application/json":
                raise WorkspaceError("Content-Type 必须是 application/json")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not set(payload) <= allowed:
                raise WorkspaceError(f"请求字段无效，只允许：{', '.join(sorted(allowed)) or '无'}")
            return payload

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return hmac.compare_digest(supplied.encode(), expected.encode())

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            parsed = urlsplit(origin)
            host = self.headers.get("Host", "").lower()
            return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._bytes(status, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                        "application/json; charset=utf-8")

        def _bytes(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.timeout = 0.1
    if ready_event is not None:
        ready_event.set()
    try:
        if stop_event is None:
            server.serve_forever(poll_interval=0.25)
        else:
            while not stop_event.is_set():
                server.handle_request()
    finally:
        server.server_close()
        controller.close()


def _bounded_integer(
    values: dict[str, list[str]],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    items = values.get(name)
    if items is None:
        return default
    if len(items) != 1:
        raise WorkspaceError(f"{name} 只能出现一次")
    value = int(items[0])
    if not minimum <= value <= maximum:
        raise WorkspaceError(f"{name} 超出允许范围")
    return value


def _command_http_status(result: dict[str, Any]) -> HTTPStatus:
    if result.get("status") == "queued":
        return HTTPStatus.ACCEPTED
    if result.get("status") == "failed":
        return HTTPStatus.BAD_GATEWAY
    return HTTPStatus.OK
