"""Phase 5 的最小远程控制入口：回环监听、Bearer 认证、只读 Codex Runtime。"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agent_runtime.contracts import AgentRunRequest, RuntimeOperationResult, RuntimeSessionRef
from .agent_runtime.events import RuntimeEventStore
from .agent_runtime.ports import AgentRuntimePort
from .composition import create_runtime
from .dashboard import CommandQueue, DashboardService
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
        self.workspace = workspace
        self.requirement_id = requirement_id
        self.session_id = session_id
        self._queue_session_id = session_id or "new-codex-session"
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
        self._lock = threading.RLock()
        workspace.load(requirement_id)
        if session_id is not None and not session_id.strip():
            raise WorkspaceError("Codex Session 不能为空")

    def status(self, *, after: int = 0) -> dict[str, Any]:
        with self._lock:
            thread: dict[str, Any] | None = None
            if self._runtime is not None and self._session is not None:
                result = self._runtime.read_session(self._session)
                thread = result.data.get("thread") if result.ok else {
                    "error": result.error.to_dict() if result.error else "读取失败"
                }
            snapshot = self.dashboard.requirement(
                self.requirement_id, run_id=self.run_id, after=after, limit=500,
            )
            snapshot.update(
                session_id=self.session_id,
                run_id=self.run_id,
                connected=self._session is not None,
                thread=thread,
                commands=[asdict(item) for item in self.commands.history(
                    requirement_id=self.requirement_id,
                )[-100:]],
                safety={"sandbox": "read-only", "approvals": "deny"},
            )
            return snapshot

    def message(self, text: str, *, command_id: str | None = None) -> dict[str, Any]:
        message = _validate_message(text)
        command = self.commands.enqueue(
            self.requirement_id, self._queue_session_id, message, command_id=command_id,
        )
        if command.status != "queued":
            return asdict(command)
        with self._lock:
            self.commands.update(command.command_id, "delivered")
            try:
                operation = self._start_or_send(message)
                if not operation.ok or operation.session is None or operation.turn_id is None:
                    detail = operation.error.message if operation.error else "Runtime 未返回 turn"
                    failed = self.commands.update(command.command_id, "failed", detail)
                    return asdict(failed)
                assert self._runtime is not None
                result = self._runtime.wait(
                    operation.session, operation.turn_id, timeout_seconds=self.wait_seconds,
                )
                if result.returncode != 0:
                    detail = result.error.message if result.error else result.stderr or "轮次失败"
                    failed = self.commands.update(command.command_id, "failed", detail)
                    return asdict(failed)
                completed = self.commands.update(
                    command.command_id, "completed", result.summary or result.stdout,
                )
                return asdict(completed)
            except (WorkspaceError, RuntimeError, OSError, ValueError) as exc:
                failed = self.commands.update(command.command_id, "failed", str(exc))
                return asdict(failed)

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


def serve_remote_control(
    controller: RemoteController,
    *,
    token: str,
    host: str = "127.0.0.1",
    port: int = 8765,
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
                self._bytes(HTTPStatus.OK, _DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "需要有效 Bearer token"})
                return
            if path == "/api/status":
                after = 0
                if query.startswith("after="):
                    try:
                        after = max(0, int(query[6:]))
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "after 必须是整数"})
                        return
                self._json(HTTPStatus.OK, controller.status(after=after))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})

        def do_POST(self) -> None:
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "需要有效 Bearer token"})
                return
            if self.path != "/api/message":
                self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise WorkspaceError("请求体大小无效")
                if self.headers.get_content_type() != "application/json":
                    raise WorkspaceError("Content-Type 必须是 application/json")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or not set(payload) <= {"message", "command_id"}:
                    raise WorkspaceError("请求只允许 message 和 command_id")
                message = payload.get("message")
                command_id = payload.get("command_id")
                if not isinstance(message, str):
                    raise WorkspaceError("message 必须是字符串")
                if command_id is not None and not isinstance(command_id, str):
                    raise WorkspaceError("command_id 必须是字符串")
                result = controller.message(message, command_id=command_id)
                status = HTTPStatus.OK if result["status"] == "completed" else HTTPStatus.BAD_GATEWAY
                self._json(status, result)
            except (WorkspaceError, ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return hmac.compare_digest(supplied.encode(), expected.encode())

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
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        controller.close()


_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Dev OS 远程控制</title><style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;background:#0b1020;color:#e8eefc}
input,textarea,button{font:inherit;border-radius:8px;border:1px solid #34415f;padding:.7rem;background:#121a2d;color:#fff}
input,textarea{box-sizing:border-box;width:100%}textarea{min-height:110px}button{cursor:pointer;background:#2962ff}
.card{background:#121a2d;padding:1rem;border-radius:12px;margin:1rem 0}.muted{color:#9fb0d0}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style></head><body><h1>AI Dev OS 远程控制</h1>
<div class="card"><label>访问 token</label><input id="token" type="password" autocomplete="off"><button onclick="connect()">连接</button><p id="meta" class="muted">尚未连接</p></div>
<div class="card"><label>发送给固定 Codex 会话（只读沙箱、审批拒绝）</label><textarea id="message"></textarea><button onclick="sendMessage()">发送并等待回复</button><pre id="result"></pre></div>
<div class="card"><h2>对话与事件</h2><pre id="conversation">连接后显示</pre></div>
<script>
const token=document.getElementById('token'); token.value=sessionStorage.getItem('ai-dev-os-token')||'';
function headers(){return {'Authorization':'Bearer '+token.value,'Content-Type':'application/json'}}
async function request(path,opts={}){const r=await fetch(path,{...opts,headers:headers()});const j=await r.json();if(!r.ok)throw Error(j.error||j.result||r.status);return j}
function textFromThread(thread){if(!thread)return '尚未建立 Runtime 连接';const turns=thread.turns||[];return turns.map(t=>JSON.stringify(t,null,2)).join('\n\n')||JSON.stringify(thread,null,2)}
async function connect(){sessionStorage.setItem('ai-dev-os-token',token.value);try{const s=await request('api/status');meta.textContent=s.requirement_id+' / '+s.session_id+' / '+(s.connected?'已连接':'等待首条消息');conversation.textContent=textFromThread(s.thread)+'\n\n事件：\n'+JSON.stringify(s.events,null,2)}catch(e){meta.textContent='连接失败：'+e.message}}
async function sendMessage(){result.textContent='发送中…';try{const r=await request('api/message',{method:'POST',body:JSON.stringify({message:message.value,command_id:crypto.randomUUID()})});result.textContent=r.result||r.status;await connect()}catch(e){result.textContent='失败：'+e.message}}
setInterval(()=>{if(token.value)connect()},5000);
</script></body></html>"""
