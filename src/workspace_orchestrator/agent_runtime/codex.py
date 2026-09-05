"""Codex App Server 的长期 stdio Adapter；不操作 Requirement/Task 绑定。"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .contracts import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    EventSink,
    ModelDescriptor,
    OperationStatus,
    RuntimeDescriptor,
    RuntimeFailure,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from .stdio import JsonObject, JsonRpcStdioClient, RpcResponseError, RpcTransportError

_CAPABILITIES = (
    "start", "resume", "read", "message", "steer", "interrupt", "archive",
    "events", "models", "approval_response",
)
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval"
}


def codex_command(executable: str | None = None) -> tuple[str, ...]:
    """解析官方 npm launcher，不把 Windows 批处理脚本交给隐式 shell。"""
    selected = executable or os.environ.get("AI_DEV_OS_CODEX") or shutil.which("codex")
    if not selected:
        raise RpcTransportError("unavailable", "未找到 codex CLI")
    selected = shutil.which(selected) or selected
    if sys.platform == "win32" and Path(selected).suffix.lower() in {".cmd", ".ps1", ".bat"}:
        script = Path(selected).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = Path(selected).parent / "node.exe"
        node_executable = str(node) if node.is_file() else shutil.which("node")
        if not script.is_file() or not node_executable:
            raise RpcTransportError("unavailable", "Codex npm launcher 缺失，请配置原生 codex.exe")
        return node_executable, str(script)
    return (selected,)


def initialize_codex(client: JsonRpcStdioClient, *, timeout: float = 30) -> JsonObject:
    """严格先等 initialize 成功，再 initialized；不在握手前流水线发其他方法。"""
    try:
        client_version = version("ai-dev-os")
    except PackageNotFoundError:
        client_version = "unknown"
    result = client.request("initialize", {
        "clientInfo": {"name": "ai_dev_os", "title": "AI Dev OS", "version": client_version},
        "capabilities": {"experimentalApi": False},
    }, timeout=timeout)
    client.notify("initialized")
    return result


class CodexRuntime:
    """每个实例拥有一个会话的连接；审批默认拒绝，defer 模式供显式控制面响应。"""

    runtime_id = "codex"

    def __init__(
        self,
        executable: str | None = None,
        *,
        command: Sequence[str] | None = None,
        event_sink: EventSink | None = None,
        timeout_seconds: float = 30,
        client_factory: Callable[..., JsonRpcStdioClient] = JsonRpcStdioClient,
        environ: Mapping[str, str] | None = None,
        approval_mode: str = "deny",
    ) -> None:
        if approval_mode not in {"deny", "defer"}:
            raise ValueError("approval_mode 必须为 deny 或 defer")
        self.executable, self.command = executable, tuple(command) if command else None
        self.event_sink, self.timeout_seconds = event_sink, timeout_seconds
        self.client_factory, self.environ = client_factory, environ
        self.approval_mode = approval_mode
        self._client: JsonRpcStdioClient | None = None
        self._initialized: JsonObject = {}
        self._bypass = False
        self._request: AgentRunRequest | None = None
        self._session: RuntimeSessionRef | None = None
        self._resumed = False
        self._condition = threading.Condition(threading.RLock())
        self._turns: dict[str, JsonObject] = {}
        self._pending_approvals: dict[int | str, JsonObject] = {}
        self._discovery_id = f"discovery-{uuid.uuid4()}"

    def _connect(self, request: AgentRunRequest | None = None) -> JsonRpcStdioClient:
        bypass = bool(request and request.bypass_hook_trust)
        if self._client is not None and bypass != self._bypass and self._session is None:
            self._client.close()
            self._client = None
        if self._client is None:
            environment = dict(self.environ) if self.environ is not None else os.environ.copy()
            for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI"):
                environment.pop(name, None)
            environment["AI_DEV_OS_DISPATCHER_CHILD"] = "1"
            if self.command:
                command = self.command
            else:
                command = (
                    *codex_command(self.executable),
                    *(("--dangerously-bypass-hook-trust",) if bypass else ()),
                    "app-server", "--listen", "stdio://",
                )
            client = self.client_factory(
                command, cwd=request.workspace_path if request else None,
                environ=environment, on_notification=self._on_notification,
                on_server_request=self._on_server_request, on_error=self._on_error,
            )
            self._client, self._bypass = client, bypass
            try:
                client.start()
                self._initialized = initialize_codex(client, timeout=self.timeout_seconds)
            except (RpcTransportError, RpcResponseError):
                client.close()
                self._client = None
                raise
        return self._client

    def _on_error(self, error: RpcTransportError) -> None:
        with self._condition:
            self._condition.notify_all()

    def _emit(self, kind: str, message: JsonObject, session_id: str | None, turn_id: str | None) -> None:
        if self.event_sink:
            self.event_sink(AgentEvent(
                event_id=str(uuid.uuid4()),
                run_id=self._request.run_id if self._request else self._discovery_id,
                runtime_id="codex", kind=kind, payload=message,
                session_id=session_id, turn_id=turn_id,
            ))

    def _on_notification(self, message: JsonObject) -> None:
        params = message.get("params") or {}
        method = message["method"]
        if not isinstance(params, dict):
            raise RpcTransportError("protocol_error", "通知 params 必须是对象")
        thread = params.get("thread") or {}
        turn = params.get("turn") or {}
        item = params.get("item") or {}
        session_id = params.get("threadId") or thread.get("id")
        turn_id = params.get("turnId") or turn.get("id")
        kind = "unknown"
        if method.startswith("thread/"):
            kind = "session"
        elif method == "turn/completed":
            kind = "completion"
        elif method.startswith("turn/"):
            kind = "turn"
        elif method == "error":
            kind = "error"
        elif method == "item/agentMessage/delta" or item.get("type") in {"agentMessage", "userMessage"}:
            kind = "message"
        elif method.startswith("item/"):
            kind = "tool"
        with self._condition:
            self._emit(kind, message, session_id, turn_id)
            # 外部通知保真，但只有已拥有会话的真实 turn 事实能完成当前执行。
            owned = self._session is not None and session_id == self._session.session_id
            if owned and turn_id:
                state = self._turns.setdefault(turn_id, {"deltas": [], "messages": {}})
                if method == "turn/completed":
                    state["completed"] = turn
                    self._pending_approvals = {
                        key: value for key, value in self._pending_approvals.items()
                        if value["params"].get("turnId") != turn_id
                    }
                elif method == "item/agentMessage/delta":
                    state["deltas"].append(str(params.get("delta", "")))
                elif method == "item/completed" and item.get("type") == "agentMessage":
                    state["messages"][str(item.get("id", "message"))] = str(item.get("text", ""))
            if method == "serverRequest/resolved":
                request_id = params.get("requestId")
                if isinstance(request_id, (str, int)):
                    self._pending_approvals.pop(request_id, None)
            self._condition.notify_all()

    def _on_server_request(self, message: JsonObject) -> None:
        params = message.get("params") or {}
        session_id, turn_id = params.get("threadId"), params.get("turnId")
        assert self._client is not None
        with self._condition:
            owned = self._session is not None and session_id == self._session.session_id
            known = message["method"] in _APPROVAL_METHODS
            self._emit("approval" if known else "unknown", message, session_id, turn_id)
            if known and owned and self.approval_mode == "defer":
                self._pending_approvals[message["id"]] = message
                self._condition.notify_all()
                return
            if known:
                self._client.respond(message["id"], {"decision": "decline"})
            else:
                self._client.respond(message["id"], error={
                    "code": -32601, "message": "客户端未声明支持此服务端请求"
                })

    @staticmethod
    def _failure(exc: RpcTransportError | RpcResponseError) -> RuntimeOperationResult:
        if isinstance(exc, RpcTransportError):
            status: OperationStatus = "timeout" if exc.code == "timeout" else (
                "unavailable" if exc.code == "unavailable" else "failed"
            )
            return RuntimeOperationResult(status=status, error=RuntimeFailure(exc.code, str(exc)))
        code = "unsupported" if exc.code == -32601 else "rpc_error"
        # 只识别明确的会话不存在；认证、模型、网络或执行错误都不得触发 prompt 重放。
        detail = str(exc).lower()
        if exc.code in {-32600, -32000} and (
            "no rollout found for thread id" in detail or detail.startswith("thread not found:")
        ):
            code = "session_missing"
        return RuntimeOperationResult(
            status="unsupported" if code == "unsupported" else "failed",
            error=RuntimeFailure(code, str(exc), details={"rpc_error": exc.error}),
        )

    def describe(self) -> RuntimeDescriptor:
        try:
            client = self._connect(self._request)
            models: list[ModelDescriptor] = []
            cursor: str | None = None
            cursors: set[str] = set()
            while True:
                result = client.request("model/list", {
                    "limit": 100, "includeHidden": False, "cursor": cursor
                }, timeout=self.timeout_seconds)
                entries = result.get("data")
                if not isinstance(entries, list):
                    raise RpcTransportError("protocol_error", "model/list data 必须是数组")
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
                        raise RpcTransportError("protocol_error", "Model 缺少真实模型 ID")
                    efforts = entry.get("supportedReasoningEfforts", [])
                    if not isinstance(efforts, list) or any(
                        not isinstance(item, dict) or not isinstance(item.get("reasoningEffort"), str)
                        for item in efforts
                    ):
                        raise RpcTransportError("protocol_error", "Model reasoning efforts 无效")
                    models.append(ModelDescriptor(
                        id=entry["model"], name=str(entry.get("displayName") or entry["model"]),
                        reasoning_efforts=tuple(
                            item["reasoningEffort"] for item in efforts
                        ), is_default=bool(entry.get("isDefault")), metadata=entry,
                    ))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str):
                    raise RpcTransportError("protocol_error", "Model 分页游标必须是字符串")
                if cursor in cursors:
                    raise RpcTransportError("protocol_error", "model/list 返回重复分页游标")
                cursors.add(cursor)
            return RuntimeDescriptor(
                "codex", "Codex App Server", str(self._initialized.get("userAgent", "unknown")),
                True, _CAPABILITIES, tuple(models),
            )
        except (RpcTransportError, RpcResponseError) as exc:
            return RuntimeDescriptor("codex", "Codex App Server", "unknown", False, reason=str(exc))

    def _begin(self, request: AgentRunRequest, *, resumed: bool) -> RuntimeOperationResult:
        if self._session is not None:
            return RuntimeOperationResult("failed", error=RuntimeFailure("busy", "实例已拥有会话"))
        if resumed and not request.resume_session_id:
            return RuntimeOperationResult("failed", error=RuntimeFailure(
                "invalid_request", "resume 必须提供 resume_session_id"
            ))
        self._request = request
        try:
            client = self._connect(request)
            params: JsonObject = {
                "cwd": str(request.workspace_path.resolve()), "sandbox": request.sandbox,
                "approvalPolicy": "on-request", "approvalsReviewer": "user",
            }
            if request.model:
                params["model"] = request.model
            if resumed:
                params["threadId"] = request.resume_session_id
            result = client.request(
                "thread/resume" if resumed else "thread/start", params, timeout=self.timeout_seconds
            )
            thread = result.get("thread")
            if not isinstance(thread, dict):
                raise RpcTransportError("protocol_error", "Thread 响应必须包含对象")
            session_id = thread.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise RpcTransportError("protocol_error", "Thread 响应缺少真实 session ID")
            if resumed and session_id != request.resume_session_id:
                raise RpcTransportError("protocol_error", "resume 响应 Thread ID 不匹配")
            self._session = RuntimeSessionRef(
                "codex", session_id, request.run_id, str(request.workspace_path.resolve())
            )
            self._resumed = resumed
            return self.send_message(self._session, request.prompt)
        except (RpcTransportError, RpcResponseError) as exc:
            return self._failure(exc)

    def start(self, request: AgentRunRequest) -> RuntimeOperationResult:
        return self._begin(request, resumed=False)

    def resume(self, request: AgentRunRequest) -> RuntimeOperationResult:
        return self._begin(request, resumed=True)

    def _scope(self, session: RuntimeSessionRef, turn_id: str | None = None) -> None:
        if session != self._session or session.runtime_id != "codex":
            raise RpcTransportError("scope_mismatch", "会话引用不属于此 Runtime 实例")
        if turn_id is not None and turn_id not in self._turns:
            raise RpcTransportError("scope_mismatch", "Turn 不属于此 Runtime 会话")

    def _operation(
        self, session: RuntimeSessionRef, method: str, params: JsonObject,
        *, turn_id: str | None = None,
    ) -> RuntimeOperationResult:
        try:
            self._scope(session, turn_id)
            assert self._client is not None
            result = self._client.request(method, {
                "threadId": session.session_id, **params
            }, timeout=self.timeout_seconds)
            return RuntimeOperationResult("ok", session=session, turn_id=turn_id, data=result)
        except (RpcTransportError, RpcResponseError) as exc:
            return self._failure(exc)

    def read_session(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        result = self._operation(session, "thread/read", {"includeTurns": True})
        if result.ok:
            thread = result.data.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != session.session_id:
                return RuntimeOperationResult("failed", session=session, error=RuntimeFailure(
                    "protocol_error", "thread/read 响应身份与请求不一致"
                ))
        return result

    def send_message(self, session: RuntimeSessionRef, text: str) -> RuntimeOperationResult:
        result = self._operation(session, "turn/start", {
            "input": [{"type": "text", "text": text}],
        })
        if not result.ok:
            return result
        turn = result.data.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            return RuntimeOperationResult("failed", session=session, error=RuntimeFailure(
                "protocol_error", "turn/start 响应缺少真实 turn ID"
            ))
        with self._condition:
            self._turns.setdefault(turn_id, {"deltas": [], "messages": {}})
        return RuntimeOperationResult("ok", session=session, turn_id=turn_id, data=result.data)

    def steer(self, session: RuntimeSessionRef, turn_id: str, text: str) -> RuntimeOperationResult:
        return self._operation(session, "turn/steer", {
            "expectedTurnId": turn_id, "input": [{"type": "text", "text": text}],
        }, turn_id=turn_id)

    def interrupt(self, session: RuntimeSessionRef, turn_id: str) -> RuntimeOperationResult:
        return self._operation(session, "turn/interrupt", {"turnId": turn_id}, turn_id=turn_id)

    def archive(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return self._operation(session, "thread/archive", {})

    def respond_to_request(
        self, session: RuntimeSessionRef, request_id: str | int, decision: dict[str, Any]
    ) -> RuntimeOperationResult:
        try:
            self._scope(session)
            with self._condition:
                request = self._pending_approvals.get(request_id)
                if not request or request["params"].get("threadId") != session.session_id:
                    raise RpcTransportError("scope_mismatch", "审批请求不存在或不属于此会话")
                turn_id = request["params"].get("turnId")
                self._scope(session, turn_id)
                if "completed" in self._turns[turn_id]:
                    raise RpcTransportError("scope_mismatch", "审批请求对应轮次已结束")
                if set(decision) != {"decision"} or decision["decision"] not in (
                    "accept", "acceptForSession", "decline", "cancel"
                ):
                    raise RpcTransportError("invalid_request", "不支持此审批响应")
                available = request["params"].get("availableDecisions")
                if available is not None and decision["decision"] not in available:
                    raise RpcTransportError("invalid_request", "审批决策不在服务端声明选项中")
                assert self._client is not None
                self._client.respond(request_id, decision)
                del self._pending_approvals[request_id]
            return RuntimeOperationResult("ok", session=session)
        except RpcTransportError as exc:
            return self._failure(exc)

    def wait(
        self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float
    ) -> AgentRunResult:
        failure: RuntimeFailure | None = None
        returncode = 1
        summary = ""
        try:
            self._scope(session, turn_id)
            deadline = time.monotonic() + timeout_seconds
            with self._condition:
                state = self._turns[turn_id]
                while "completed" not in state:
                    assert self._client is not None
                    if self._client.failure:
                        raise self._client.failure
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RpcTransportError("timeout", "等待 Codex turn/completed 超时")
                    self._condition.wait(remaining)
                completion = state["completed"]
                status = completion.get("status")
                returncode = 0 if status == "completed" else (130 if status == "interrupted" else 1)
                summary = "\n".join(state["messages"].values()) or "".join(state["deltas"])
                if returncode:
                    failure = RuntimeFailure(
                        "interrupted" if returncode == 130 else "turn_failed",
                        "Codex 轮次未成功完成", details={"turn": completion},
                    )
        except RpcTransportError as exc:
            returncode = 124 if exc.code == "timeout" else 1
            failure = RuntimeFailure(exc.code, str(exc))
        return AgentRunResult(
            returncode, session.session_id, summary,
            self._client.stderr_tail if self._client else "", self._resumed,
            runtime_id="codex", run_id=session.run_id, summary=summary, error=failure,
        )

    def close(self) -> None:
        if self._client:
            self._client.close()
