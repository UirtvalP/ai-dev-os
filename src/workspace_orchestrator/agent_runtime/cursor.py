"""Cursor ACP v1 适配器：协商能力、流式轮次和默认拒绝的权限边界。

协议核验：2026-09-05，https://cursor.com/docs/cli/acp 及
https://agentclientprotocol.com/protocol/v1/session-setup 。不复制第三方实现。
一个实例持有一个活动 Session；完成的轮次不会完成 Requirement。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

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
    standard_event_kind,
)
from .stdio import JsonRpcStdioClient, RpcResponseError, RpcTransportError


class CursorAcpRuntime:
    """使用实际 ACP 响应，不将 CLI 存在、取消请求或进程退出伪装成成功。"""

    runtime_id = "cursor"

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        executable: str | None = None,
        event_sink: EventSink | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 30,
        client_factory: Callable[..., JsonRpcStdioClient] = JsonRpcStdioClient,
    ) -> None:
        self.command = tuple(command) if command is not None else (
            executable or shutil.which("agent") or shutil.which("cursor-agent") or "agent", "acp"
        )
        self.event_sink = event_sink
        self.environ = dict(environ) if environ is not None else dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CLAUDE_SESSION_ID"):
            self.environ.pop(key, None)
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory
        self._client: JsonRpcStdioClient | None = None
        self._request: AgentRunRequest | None = None
        self._session: RuntimeSessionRef | None = None
        self._condition = threading.Condition(threading.RLock())
        self._active_turn: str | None = None
        self._results: dict[str, AgentRunResult] = {}
        self._messages: dict[str, list[str]] = {}
        self._raw: dict[str, list[dict[str, Any]]] = {}
        self._capabilities: dict[str, Any] = {}
        self._config: list[dict[str, Any]] = []
        self._models: tuple[ModelDescriptor, ...] = ()
        self._version = "未协商"
        self._closed = False
        self._discovery_attempted = False
        self._discovery_error: str | None = None
        self._discovery: CursorAcpRuntime | None = None
        self._discovery_root: TemporaryDirectory[str] | None = None
        self._discovery_lock = threading.RLock()

    def describe(self) -> RuntimeDescriptor:
        installed = self._installed()
        with self._discovery_lock:
            if installed and not self._closed and self._client is None and not self._discovery_attempted:
                self._discover()
        available = installed and not self._closed and bool(self._models) and self._discovery is None
        capabilities = ["start", "message", "interrupt", "events", "models", "profile:read-only"]
        if self._capabilities.get("loadSession") is True:
            capabilities.append("resume")
        reason = "仅支持 ACP ask 只读模式和权限拒绝，不提供 OS 级读隔离"
        if not installed:
            reason = "未找到 Cursor Agent CLI；未安装或未配置可执行路径"
        elif not available:
            reason = self._discovery_error or "Cursor 已关闭或未报告可用模型"
        return RuntimeDescriptor(
            self.runtime_id, "Cursor ACP", self._version, available,
            tuple(capabilities), self._models if available else (), reason,
        )

    def _installed(self) -> bool:
        return bool(self.command and shutil.which(self.command[0]))

    def _close_discovery(self) -> None:
        # 关闭失败仍保留拥有者，禁止描述成功或另起实际 Worker；close() 可以重试清理。
        if self._discovery is not None:
            self._discovery.close()
        if self._discovery_root is not None:
            self._discovery_root.cleanup()
        self._discovery = None
        self._discovery_root = None

    def _discover(self) -> None:
        """ACP 的模型配置在 session/new 返回；独立会话不发送任何 prompt。

        https://agentclientprotocol.com/protocol/v1/session-config-options
        """
        self._discovery_attempted = True
        probe = CursorAcpRuntime(
            self.command, environ=self.environ, timeout_seconds=self.timeout_seconds,
            client_factory=self.client_factory,
        )
        self._discovery = probe
        try:
            self._discovery_root = TemporaryDirectory(prefix="ai-dev-os-cursor-discovery-")
            try:
                root = Path(self._discovery_root.name)
                failed = probe._connect(root)
                if failed:
                    assert failed.error
                    raise RpcTransportError(failed.error.code, failed.error.message)
                result = probe._connection().request(
                    "session/new", {"cwd": str(root.resolve()), "mcpServers": []},
                    timeout=self.timeout_seconds,
                )
                if not isinstance(result.get("sessionId"), str) or not result["sessionId"]:
                    raise RpcTransportError("protocol_error", "Cursor 发现未返回有效 sessionId")
                probe._update_models(result)
                if not probe._models:
                    raise RpcTransportError("protocol_error", "Cursor 发现未报告可用模型")
            finally:
                self._close_discovery()
            self._models, self._version = probe._models, probe._version
            self._capabilities = probe._capabilities
        except Exception as exc:  # noqa: BLE001 -- 探测失败不能阻断其他 Runtime 发现或伪造能力。
            self._discovery_error = f"Cursor 模型发现失败：{exc}"

    def _failure(self, code: str, message: str, **details: Any) -> RuntimeOperationResult:
        status: OperationStatus = "unsupported" if code == "unsupported" else (
            "unavailable" if code == "unavailable" else "timeout" if code == "timeout" else "failed"
        )
        return RuntimeOperationResult(
            status, session=self._session,
            error=RuntimeFailure(code, message, code in ("timeout", "unavailable"), details),
        )

    def _exception(self, error: RpcResponseError | RpcTransportError) -> RuntimeOperationResult:
        if isinstance(error, RpcTransportError):
            return self._failure(error.code, str(error))
        # ACP 的服务器错误没有跨实现统一的 missing-session 错误码；不能猜测并重放。
        return self._failure("provider_error", str(error), provider_error=error.error)

    def _emit(self, kind: str, payload: dict[str, Any], turn_id: str | None = None) -> None:
        if self.event_sink and self._request:
            self.event_sink(AgentEvent(
                event_id=str(uuid4()), run_id=self._request.run_id,
                runtime_id=self.runtime_id, kind=standard_event_kind(kind), payload=payload,
                extra={"detail_kind": kind},
                session_id=self._session.session_id if self._session else None,
                turn_id=turn_id,
            ))

    def _connect(self, workspace_path: Path) -> RuntimeOperationResult | None:
        if not self._installed():
            return self._failure("unavailable", "未找到 Cursor Agent CLI")
        # 实际连接必须重新协商，不能使用发现连接的模型/配置作为本次会话事实。
        self._models, self._config, self._capabilities = (), [], {}
        self._client = self.client_factory(
            self.command, cwd=workspace_path, environ=self.environ,
            jsonrpc=True, on_notification=self._notification,
            on_server_request=self._server_request,
        )
        self._client.start()
        initialized = self._client.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
            },
            "clientInfo": {"name": "ai-dev-os", "version": "1"},
        }, timeout=self.timeout_seconds)
        if type(initialized.get("protocolVersion")) is not int or initialized["protocolVersion"] != 1:
            return self._failure("unsupported", "Cursor ACP 协议版本不是支持的 v1")
        capabilities = initialized.get("agentCapabilities", {})
        self._capabilities = capabilities if isinstance(capabilities, dict) else {}
        info = initialized.get("agentInfo", {})
        if isinstance(info, dict):
            self._version = str(info.get("version", "未报告"))
        auth = initialized.get("authMethods", [])
        if any(isinstance(item, dict) and item.get("id") == "cursor_login" for item in auth):
            self._client.request("authenticate", {"methodId": "cursor_login"},
                                 timeout=self.timeout_seconds)
        return None

    def _connection(self) -> JsonRpcStdioClient:
        if self._client is None:
            raise RpcTransportError("closed", "Cursor 连接尚未启动")
        return self._client

    def start(self, request: AgentRunRequest) -> RuntimeOperationResult:
        with self._discovery_lock:
            return self._open(request, resumed=False)

    def resume(self, request: AgentRunRequest) -> RuntimeOperationResult:
        if not request.resume_session_id:
            return self._failure("invalid_request", "恢复 Cursor Session 必须提供 session_id")
        with self._discovery_lock:
            return self._open(request, resumed=True)

    def _open(self, request: AgentRunRequest, *, resumed: bool) -> RuntimeOperationResult:
        if self._closed or self._client is not None or self._discovery is not None:
            return self._failure("invalid_state", "该 Runtime 已启动或已关闭，请使用独立实例")
        if not self._installed():
            return self._failure("unavailable", "未找到 Cursor Agent CLI")
        if request.bypass_hook_trust:
            return self._failure("unsupported", "Cursor ACP 不支持绕过 Hook 信任")
        if request.reasoning_effort is not None:
            return self._failure("unsupported", "Cursor ACP 尚未协商 reasoning effort 控制")
        if request.sandbox != "read-only":
            return self._failure("unsupported", "Cursor ACP 不能提供所请求的 OS sandbox",
                                 sandbox=request.sandbox)
        try:
            self._request = request
            failed = self._connect(request.workspace_path)
            if failed:
                self.close()
                return failed
            client = self._connection()
            params: dict[str, Any] = {"cwd": str(request.workspace_path.resolve()), "mcpServers": []}
            if resumed:
                if self._capabilities.get("loadSession") is not True:
                    self.close()
                    return self._failure("unsupported", "Cursor 未协商 loadSession 能力")
                params["sessionId"] = request.resume_session_id
                self._session = RuntimeSessionRef(
                    self.runtime_id, str(request.resume_session_id), request.run_id,
                    str(request.workspace_path),
                )
            result = client.request("session/load" if resumed else "session/new", params,
                                          timeout=self.timeout_seconds)
            session_id = request.resume_session_id if resumed else result.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise RpcTransportError("protocol_error", "Cursor 未返回有效 sessionId")
            self._session = RuntimeSessionRef(
                self.runtime_id, session_id, request.run_id, str(request.workspace_path),
            )
            self._update_models(result)
            if request.sandbox == "read-only":
                # ACP ask 是 Provider 的只读模式，不宣称是操作系统级沙箱。
                client.request("session/set_mode", {"sessionId": session_id, "modeId": "ask"},
                                     timeout=self.timeout_seconds)
            if request.model:
                model_config = next((item for item in self._config
                                     if item.get("category") == "model"), None)
                if model_config is None or request.model not in {item.id for item in self._models}:
                    self.close()
                    return self._failure("unsupported", "Cursor 未提供所请求模型的配置能力",
                                         model=request.model)
                configured = client.request("session/set_config_option", {
                    "sessionId": session_id, "configId": model_config["id"], "value": request.model,
                }, timeout=self.timeout_seconds)
                self._update_models(configured)
                if not any(item.id == request.model and item.is_default for item in self._models):
                    raise RpcTransportError("protocol_error", "Cursor 未确认所请求的模型")
            self._emit("session.resumed" if resumed else "session.started", result)
            return self.send_message(self._session, request.prompt)
        except (RpcResponseError, RpcTransportError) as exc:
            result_failure = self._exception(exc)
            self.close()
            return result_failure
        except Exception as exc:  # noqa: BLE001 -- 外部协议与可替换事件回调的故障边界。
            self.close()
            return self._failure("protocol_error", f"Cursor 初始化或事件发布失败：{exc}")

    def _update_models(self, payload: dict[str, Any]) -> None:
        config = payload.get("configOptions")
        if not isinstance(config, list):
            return
        self._config = [item for item in config if isinstance(item, dict)]
        models: list[ModelDescriptor] = []
        for item in self._config:
            if item.get("category") != "model" or item.get("type") != "select":
                continue
            for option in item.get("options", []):
                if isinstance(option, dict) and isinstance(option.get("value"), str):
                    if not option["value"].strip() or any(model.id == option["value"] for model in models):
                        raise RpcTransportError("protocol_error", "Cursor 模型 ID 为空或重复")
                    models.append(ModelDescriptor(
                        option["value"], str(option.get("name", option["value"])),
                        is_default=option["value"] == item.get("currentValue"), metadata=option,
                    ))
        self._models = tuple(models)

    def _notification(self, message: dict[str, Any]) -> None:
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise TypeError("ACP 通知 params 必须是对象")
        if self._session and params.get("sessionId", self._session.session_id) != self._session.session_id:
            raise ValueError("ACP 通知属于其他 Session")
        update = params.get("update", {})
        if not isinstance(update, dict):
            raise TypeError("ACP update 必须是对象")
        update_type = update.get("sessionUpdate")
        kind = {
            "agent_message_chunk": "message.delta", "user_message_chunk": "message.user",
            "agent_thought_chunk": "message.reasoning", "tool_call": "tool.started",
            "tool_call_update": "tool.updated",
        }.get(update_type, "provider.unknown") if isinstance(update_type, str) else "provider.unknown"
        with self._condition:
            turn = self._active_turn
            if turn:
                self._raw[turn].append(message)
                content = update.get("content", {})
                if update_type == "agent_message_chunk" and isinstance(content, dict):
                    text = content.get("text")
                    if isinstance(text, str):
                        self._messages[turn].append(text)
            if update_type == "config_option_update":
                self._update_models(update)
            self._emit(kind, message, turn)

    def _server_request(self, message: dict[str, Any]) -> None:
        assert self._client is not None
        method = message.get("method")
        params = message.get("params", {})
        self._emit("approval.requested" if method == "session/request_permission"
                   else "provider.request", message, self._active_turn)
        response: dict[str, Any]
        if method == "session/request_permission":
            options = params.get("options", []) if isinstance(params, dict) else []
            reject = next((item.get("optionId") for item in options
                           if isinstance(item, dict) and item.get("kind") == "reject_once"
                           and isinstance(item.get("optionId"), str)), None)
            response = {"outcome": {"outcome": "selected", "optionId": reject}} if reject else {
                "outcome": {"outcome": "cancelled"}
            }
        elif method == "cursor/create_plan":
            response = {"outcome": {"outcome": "rejected", "reason": "未获得用户明确授权"}}
        elif method == "cursor/ask_question":
            response = {"outcome": {"outcome": "cancelled"}}
        else:
            self._client.respond(message["id"], error={"code": -32601, "message": "不支持此请求"})
            return
        self._client.respond(message["id"], response)
        self._emit("approval.resolved", {"request": message, "response": response}, self._active_turn)

    def _valid_session(self, session: RuntimeSessionRef) -> bool:
        return self._session is not None and session == self._session

    def send_message(self, session: RuntimeSessionRef, text: str) -> RuntimeOperationResult:
        with self._condition:
            if not self._valid_session(session) or not self._client or self._closed:
                return self._failure("invalid_state", "Cursor Session 未连接或引用不匹配")
            if self._active_turn:
                return self._failure("busy", "Cursor 正在执行轮次；不能把排队消息称为 steer")
            if not text.strip():
                return self._failure("invalid_request", "消息不能为空")
            turn_id = str(uuid4())
            self._active_turn = turn_id
            self._messages[turn_id], self._raw[turn_id] = [], []
            try:
                self._emit("turn.started", {"text": text, "turn_id_origin": "adapter"}, turn_id)
            except Exception as exc:  # noqa: BLE001 -- 发布失败不能伪装成已启动。
                self._active_turn = None
                return self._failure("event_sink_error", f"无法发布 Cursor 轮次事件：{exc}")
            threading.Thread(target=self._prompt, args=(session, turn_id, text), daemon=True).start()
            return RuntimeOperationResult("ok", session=session, turn_id=turn_id)

    def _prompt(self, session: RuntimeSessionRef, turn_id: str, text: str) -> None:
        assert self._client and self._request
        try:
            response = self._client.request("session/prompt", {
                "sessionId": session.session_id, "prompt": [{"type": "text", "text": text}],
            }, timeout=self._request.timeout_seconds)
            stop = response.get("stopReason")
            if stop not in ("end_turn", "cancelled", "max_tokens", "max_turn_requests", "refusal"):
                raise RpcTransportError("protocol_error", "Cursor 未返回有效 stopReason")
            code = 0 if stop == "end_turn" else 130 if stop == "cancelled" else 1
            error = None if code == 0 else RuntimeFailure(str(stop), "Cursor 轮次未正常完成", details=response)
            self._emit("turn.completed" if code == 0 else "turn.cancelled" if code == 130
                       else "turn.failed", response, turn_id)
        except (RpcResponseError, RpcTransportError) as exc:
            failure = self._exception(exc)
            error = failure.error
            code = 124 if failure.status == "timeout" else 1
            # 请求超时不等于服务端停止。关闭受控进程树后才允许结束此连接，禁止重放。
            if isinstance(exc, RpcTransportError):
                self.close()
        except Exception as exc:  # noqa: BLE001 -- 回调异常须收敛为可恢复失败。
            error = RuntimeFailure("event_sink_error", f"Cursor 事件发布失败：{exc}")
            code = 1
            self.close()
        with self._condition:
            summary = "".join(self._messages[turn_id])
            self._results[turn_id] = AgentRunResult(
                code, session.session_id,
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self._raw[turn_id]),
                self._client.stderr_tail, bool(self._request.resume_session_id), self.runtime_id,
                self._request.run_id, summary, error,
            )
            self._active_turn = None
            self._condition.notify_all()

    def wait(self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float) -> AgentRunResult:
        with self._condition:
            if not self._valid_session(session) or turn_id not in self._messages:
                return AgentRunResult(1, None, "", "轮次引用不匹配", runtime_id=self.runtime_id,
                                      error=RuntimeFailure("invalid_state", "轮次引用不匹配"))
            if not self._condition.wait_for(lambda: turn_id in self._results, timeout_seconds):
                return AgentRunResult(124, session.session_id, "", "等待超时；不代表轮次已取消",
                                      runtime_id=self.runtime_id, run_id=session.run_id,
                                      error=RuntimeFailure("timeout", "等待超时；轮次仍可能运行"))
            return self._results[turn_id]

    def interrupt(self, session: RuntimeSessionRef, turn_id: str) -> RuntimeOperationResult:
        with self._condition:
            if not self._valid_session(session) or self._active_turn != turn_id or not self._client:
                return self._failure("invalid_state", "没有匹配的活动轮次")
            try:
                self._client.notify("session/cancel", {"sessionId": session.session_id})
                self._emit("turn.interrupt_requested", {"turnId": turn_id}, turn_id)
                return RuntimeOperationResult("ok", session, turn_id,
                                              {"requested": True, "confirmed": False})
            except RpcTransportError as exc:
                return self._exception(exc)

    def steer(self, session: RuntimeSessionRef, turn_id: str, text: str) -> RuntimeOperationResult:
        return self._failure("unsupported", "Cursor ACP 未提供稳定的活动轮次 steer 方法")

    def archive(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return self._failure("unsupported", "Cursor ACP close/delete 不等同会话归档")

    def read_session(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return self._failure("unsupported", "Cursor 仅在 load 时回放历史，不提供只读 thread/read")

    def respond_to_request(
        self, session: RuntimeSessionRef, request_id: str | int, decision: dict[str, Any]
    ) -> RuntimeOperationResult:
        return self._failure("unsupported", "权限请求已默认拒绝，不接受迟到的授权回复")

    def close(self) -> None:
        self._closed = True
        if self._client:
            self._client.close()
        self._close_discovery()
