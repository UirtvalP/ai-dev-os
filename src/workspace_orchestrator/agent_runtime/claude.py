"""Claude CLI stream-json 适配器：保留 stdin、真实会话身份与 SDK 控制响应。

2026-09-05 核验官方 CLI reference、Agent SDK streaming 文档及
anthropics/claude-agent-sdk-python 的 _internal/query.py 控制消息格式。
这是独立协议实现，不复制 SDK 代码；CLI 版本漂移会结构化失败，不自动重放。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
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
from .stdio import JsonRpcStdioClient, RpcTransportError


class ClaudeCliRuntime:
    """一个长期 CLI 对应一个 Session；只开放受限工具，不宣称 OS 沙箱。"""

    runtime_id = "claude"

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
            executable or shutil.which("claude") or "claude",
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
        self._expected_session = ""
        self._condition = threading.Condition(threading.RLock())
        self._controls: dict[str, dict[str, Any] | None] = {}
        self._failure_value: RuntimeFailure | None = None
        self._active_turn: str | None = None
        self._results: dict[str, AgentRunResult] = {}
        self._raw: dict[str, list[dict[str, Any]]] = {}
        self._models: tuple[ModelDescriptor, ...] = ()
        self._version = "未协商"
        self._closed = False
        self._initialized = False
        self._discovery_attempted = False
        self._discovery_error: str | None = None
        self._discovery: ClaudeCliRuntime | None = None
        self._discovery_root: TemporaryDirectory[str] | None = None
        self._discovery_lock = threading.RLock()

    def describe(self) -> RuntimeDescriptor:
        installed = self._installed()
        with self._discovery_lock:
            if installed and not self._closed and self._client is None and not self._discovery_attempted:
                self._discover()
        available = installed and not self._closed and bool(self._models) and self._discovery is None
        reason = "仅 Read/Grep/Glob 工具与默认拒绝；不提供 OS 级读隔离"
        if not installed:
            reason = "未找到 Claude CLI；未安装或未配置可执行路径"
        elif not available:
            reason = self._discovery_error or "Claude 已关闭或未报告可用模型"
        return RuntimeDescriptor(
            self.runtime_id, "Claude CLI", self._version, available,
            ("start", "resume", "message", "interrupt", "events", "models", "profile:read-only"),
            self._models if available else (), reason,
        )

    def _installed(self) -> bool:
        return bool(self.command and shutil.which(self.command[0]))

    def _close_discovery(self) -> None:
        if self._discovery is not None:
            self._discovery.close()
        if self._discovery_root is not None:
            self._discovery_root.cleanup()
        self._discovery = None
        self._discovery_root = None

    def _discover(self) -> None:
        """SDK initialize 控制响应提供 models，无需 user 消息或 LLM 轮次。

        协议来源：anthropics/claude-agent-sdk-python 的 _internal/query.py。
        """
        self._discovery_attempted = True
        probe = ClaudeCliRuntime(
            self.command, environ=self.environ, timeout_seconds=self.timeout_seconds,
            client_factory=self.client_factory,
        )
        self._discovery = probe
        try:
            self._discovery_root = TemporaryDirectory(prefix="ai-dev-os-claude-discovery-")
            try:
                probe._connect(Path(self._discovery_root.name), session_id=str(uuid4()), discovery=True)
                if not probe._models:
                    raise RpcTransportError("protocol_error", "Claude 发现未报告可用模型")
            finally:
                self._close_discovery()
            self._models, self._version = probe._models, probe._version
        except Exception as exc:  # noqa: BLE001 -- 探测失败返回不可用，不伪造模型或影响其他 Runtime。
            self._discovery_error = f"Claude 模型发现失败：{exc}"

    def _failure(self, code: str, message: str, **details: Any) -> RuntimeOperationResult:
        status: OperationStatus = "unsupported" if code == "unsupported" else (
            "unavailable" if code == "unavailable" else "timeout" if code == "timeout" else "failed"
        )
        return RuntimeOperationResult(
            status, session=self._session,
            error=RuntimeFailure(code, message, code in ("timeout", "unavailable"), details),
        )

    def _emit(self, kind: str, payload: dict[str, Any], turn_id: str | None = None) -> None:
        if self.event_sink and self._request:
            self.event_sink(AgentEvent(
                event_id=str(uuid4()), run_id=self._request.run_id,
                runtime_id=self.runtime_id, kind=standard_event_kind(kind), payload=payload,
                extra={"detail_kind": kind},
                session_id=self._session.session_id if self._session else None,
                turn_id=turn_id,
            ))

    def _on_error(self, error: RpcTransportError) -> None:
        with self._condition:
            self._failure_value = RuntimeFailure(error.code, str(error))
            self._condition.notify_all()

    def _control(self, request: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        request_id = str(uuid4())
        with self._condition:
            self._controls[request_id] = None
        try:
            self._client.send({"type": "control_request", "request_id": request_id, "request": request})
            with self._condition:
                if not self._condition.wait_for(
                    lambda: self._controls[request_id] is not None or self._failure_value is not None,
                    self.timeout_seconds,
                ):
                    raise RpcTransportError("timeout", "Claude 控制请求超时，未确认执行结果")
                if self._failure_value:
                    raise RpcTransportError(self._failure_value.code, self._failure_value.message)
                response = self._controls[request_id]
                assert response is not None
                if response.get("subtype") == "error":
                    raise RpcTransportError("provider_error", str(response.get("error", "控制请求失败")))
                data = response.get("response")
                if response.get("subtype") != "success" or not isinstance(data, dict):
                    raise RpcTransportError("protocol_error", "Claude 控制响应缺少 success 对象")
                return data
        finally:
            with self._condition:
                self._controls.pop(request_id, None)

    def start(self, request: AgentRunRequest) -> RuntimeOperationResult:
        with self._discovery_lock:
            return self._open(request, resumed=False)

    def resume(self, request: AgentRunRequest) -> RuntimeOperationResult:
        if not request.resume_session_id:
            return self._failure("invalid_request", "恢复 Claude Session 必须提供 session_id")
        with self._discovery_lock:
            return self._open(request, resumed=True)

    def _connect(
        self, workspace_path: Path, *, session_id: str, resumed: bool = False,
        model: str | None = None, discovery: bool = False,
    ) -> dict[str, Any]:
        self._expected_session = session_id
        self._models = ()
        command = [
            *self.command, "--print", "--output-format", "stream-json", "--verbose",
            "--input-format", "stream-json", "--include-partial-messages",
            "--permission-mode", "plan", "--permission-prompt-tool", "stdio",
            "--tools", "" if discovery else "Read,Grep,Glob", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            f"--resume={session_id}" if resumed else f"--session-id={session_id}",
        ]
        if discovery:
            command.append("--no-session-persistence")
        if model:
            command.append(f"--model={model}")
        self._client = self.client_factory(
            command, cwd=workspace_path, environ=self.environ, raw_mode=True,
            on_message=self._message, on_error=self._on_error,
        )
        self._client.start()
        initialized = self._control({"subtype": "initialize", "hooks": None})
        self._initialized = True
        self._version = "未报告"  # 不能为发现版本而发送 user 消息以获取 system/init。
        self._read_models(initialized)
        return initialized

    def _open(self, request: AgentRunRequest, *, resumed: bool) -> RuntimeOperationResult:
        if self._closed or self._client is not None or self._discovery is not None:
            return self._failure("invalid_state", "该 Runtime 已启动或已关闭，请使用独立实例")
        if not self._installed():
            return self._failure("unavailable", "未找到 Claude CLI")
        if request.sandbox != "read-only":
            return self._failure("unsupported", "Claude CLI 不能提供所请求的 OS sandbox",
                                 sandbox=request.sandbox)
        if request.bypass_hook_trust:
            return self._failure("unsupported", "Claude CLI 不支持绕过 Hook 信任")
        if request.reasoning_effort is not None:
            return self._failure("unsupported", "Claude CLI 尚未协商 reasoning effort 控制")
        if not request.prompt.strip():
            return self._failure("invalid_request", "消息不能为空")
        self._request = request
        try:
            initialized = self._connect(
                request.workspace_path,
                session_id=str(request.resume_session_id) if resumed else str(uuid4()),
                resumed=resumed, model=request.model,
            )
            if request.model and request.model not in {model.id for model in self._models}:
                self.close()
                return self._failure("unsupported", "Claude 未发现指定模型；未发送首轮消息",
                                     model=request.model)
            with self._condition:
                turn = self._send_prompt(request.prompt)
                if not self._condition.wait_for(
                    lambda: self._session is not None or self._failure_value is not None,
                    self.timeout_seconds,
                ):
                    raise RpcTransportError("timeout", "Claude 未确认实际 Session 身份")
                if self._failure_value:
                    raise RpcTransportError(self._failure_value.code, self._failure_value.message)
                return RuntimeOperationResult("ok", self._session, turn,
                                              {"initialization": initialized})
        except RpcTransportError as exc:
            failure = self._failure(exc.code, str(exc))
            self.close()
            return failure
        except Exception as exc:  # noqa: BLE001 -- 外部协议与可替换事件回调的故障边界。
            self.close()
            return self._failure("protocol_error", f"Claude 初始化或事件发布失败：{exc}")

    def _read_models(self, payload: dict[str, Any]) -> None:
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise RpcTransportError("protocol_error", "Claude 模型列表必须是数组")
        result: list[ModelDescriptor] = []
        for model in models:
            if isinstance(model, dict) and isinstance(model.get("value"), str):
                if not model["value"].strip() or any(item.id == model["value"] for item in result):
                    raise RpcTransportError("protocol_error", "Claude 模型 ID 为空或重复")
                result.append(ModelDescriptor(
                    model["value"], str(model.get("displayName", model["value"])),
                    metadata=model,
                ))
        self._models = tuple(result)

    def _message(self, message: dict[str, Any]) -> None:
        with self._condition:
            msg_type = message.get("type")
            if msg_type == "control_response":
                response = message.get("response")
                if not isinstance(response, dict):
                    raise ValueError("Claude control_response 缺少对象")
                request_id = response.get("request_id")
                if isinstance(request_id, str) and request_id in self._controls:
                    if self._controls[request_id] is not None:
                        raise ValueError("Claude 控制响应重复")
                    self._controls[request_id] = response
                    self._condition.notify_all()
                else:
                    self._emit("provider.unknown", message, self._active_turn)
                return
            if msg_type == "control_request":
                self._deny_request(message)
                return
            session_id = message.get("session_id")
            if session_id and session_id != self._expected_session:
                raise ValueError("Claude 事件 Session 身份不匹配")
            if msg_type == "system" and message.get("subtype") == "init":
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError("Claude init 缺少 Session 身份")
                assert self._request
                if self._request.model and message.get("model") != self._request.model:
                    raise ValueError("Claude 未确认所请求的实际模型 ID")
                self._session = RuntimeSessionRef(
                    self.runtime_id, session_id, self._request.run_id, str(self._request.workspace_path),
                )
                self._version = str(message.get("claude_code_version", "未报告"))
                self._emit("session.resumed" if self._request.resume_session_id else "session.started",
                           message, self._active_turn)
                self._condition.notify_all()
            turn = self._active_turn
            if turn:
                self._raw[turn].append(message)
            kind = "provider.unknown"
            if msg_type == "assistant":
                kind = "message.completed"
                body = message.get("message", {})
                if isinstance(body, dict) and isinstance(body.get("content"), list):
                    for block in body["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            self._emit("tool.started", {"message": message, "block": block}, turn)
            elif msg_type == "user":
                kind = "message.user"
                body = message.get("message", {})
                if isinstance(body, dict) and isinstance(body.get("content"), list):
                    for block in body["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            self._emit("tool.completed", {"message": message, "block": block}, turn)
            elif msg_type == "stream_event":
                event = message.get("event", {})
                if isinstance(event, dict):
                    kind = "message.delta" if event.get("type") == "content_block_delta" else "provider.unknown"
                    delta = event.get("delta", {})
                    if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                        kind = "tool.delta"
                    block = event.get("content_block", {})
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        kind = "tool.started"
            elif msg_type == "result":
                if not turn or not self._session or not self._request:
                    raise ValueError("Claude result 没有对应的活动 Session/轮次")
                if type(message.get("is_error")) is not bool or not isinstance(message.get("subtype"), str):
                    raise ValueError("Claude result 缺少明确的成功/失败字段")
                success = message["is_error"] is False and message["subtype"] == "success"
                summary = message.get("result", "")
                if not isinstance(summary, str):
                    raise ValueError("Claude result 文本不是字符串")
                error = None if success else RuntimeFailure(
                    "provider_error", "Claude 轮次未成功", details=message,
                )
                kind = "turn.completed" if success else "turn.failed"
                self._emit(kind, message, turn)
                self._results[turn] = AgentRunResult(
                    0 if success else 1, self._session.session_id,
                    "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self._raw[turn]),
                    self._client.stderr_tail if self._client else "", bool(self._request.resume_session_id),
                    self.runtime_id, self._request.run_id, summary, error,
                )
                self._active_turn = None
                self._condition.notify_all()
                return
            self._emit(kind, message, turn)

    def _deny_request(self, message: dict[str, Any]) -> None:
        assert self._client
        request_id = message.get("request_id")
        request = message.get("request")
        if not isinstance(request_id, str) or not isinstance(request, dict):
            raise TypeError("Claude 请求格式无效")
        permission = request.get("subtype") == "can_use_tool"
        self._emit("approval.requested" if permission else "provider.request", message, self._active_turn)
        response = {
            "type": "control_response", "response": {
                "subtype": "success", "request_id": request_id,
                "response": {"behavior": "deny", "message": "未获得用户明确授权"},
            } if permission else {
                "subtype": "error", "request_id": request_id, "error": "不支持此控制请求",
            },
        }
        self._client.send(response)
        self._emit("approval.resolved", {"request": message, "response": response}, self._active_turn)

    def _send_prompt(self, text: str) -> str:
        assert self._client
        turn = str(uuid4())
        self._active_turn = turn
        self._raw[turn] = []
        self._emit("turn.started", {"text": text, "turn_id_origin": "adapter"}, turn)
        self._client.send({
            "type": "user", "session_id": self._expected_session,
            "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
        })
        return turn

    def send_message(self, session: RuntimeSessionRef, text: str) -> RuntimeOperationResult:
        with self._condition:
            if session != self._session or self._closed or self._failure_value:
                return self._failure("invalid_state", "Claude Session 未连接或引用不匹配")
            if self._active_turn:
                return self._failure("busy", "Claude 正在执行轮次；消息不会冒充活动轮次 steer")
            if not text.strip():
                return self._failure("invalid_request", "消息不能为空")
            try:
                turn = self._send_prompt(text)
                return RuntimeOperationResult("ok", session, turn)
            except RpcTransportError as exc:
                return self._failure(exc.code, str(exc))
            except Exception as exc:  # noqa: BLE001 -- 发布失败不能伪装成已启动。
                self._active_turn = None
                return self._failure("event_sink_error", f"无法发布 Claude 轮次事件：{exc}")

    def wait(self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float) -> AgentRunResult:
        with self._condition:
            if session != self._session or turn_id not in self._raw:
                return AgentRunResult(1, None, "", "轮次引用不匹配", runtime_id=self.runtime_id,
                                      error=RuntimeFailure("invalid_state", "轮次引用不匹配"))
            deadline = time.monotonic() + timeout_seconds
            self._condition.wait_for(
                lambda: turn_id in self._results or self._failure_value is not None,
                max(0, deadline - time.monotonic()),
            )
            if turn_id in self._results:
                return self._results[turn_id]
            error = self._failure_value or RuntimeFailure("timeout", "等待超时；轮次仍可能运行")
            return AgentRunResult(
                124 if error.code == "timeout" else 1, session.session_id, "", error.message,
                runtime_id=self.runtime_id, run_id=session.run_id, error=error,
            )

    def interrupt(self, session: RuntimeSessionRef, turn_id: str) -> RuntimeOperationResult:
        with self._condition:
            if session != self._session or self._active_turn != turn_id or not self._initialized:
                return self._failure("invalid_state", "没有匹配的活动轮次")
        try:
            response = self._control({"subtype": "interrupt"})
            self._emit("turn.interrupt_requested", {"response": response}, turn_id)
            return RuntimeOperationResult("ok", session, turn_id,
                                          {"requested": True, "acknowledged": True, "completed": False})
        except RpcTransportError as exc:
            return self._failure(exc.code, str(exc))

    def steer(self, session: RuntimeSessionRef, turn_id: str, text: str) -> RuntimeOperationResult:
        return self._failure("unsupported", "Claude 排队输入不等同活动轮次 steer")

    def archive(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return self._failure("unsupported", "Claude CLI 未提供此适配器可验证的归档协议")

    def read_session(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return self._failure("unsupported", "Claude CLI 不提供只读 thread/read 方法")

    def respond_to_request(
        self, session: RuntimeSessionRef, request_id: str | int, decision: dict[str, Any]
    ) -> RuntimeOperationResult:
        return self._failure("unsupported", "权限请求已默认拒绝，不接受迟到的授权回复")

    def close(self) -> None:
        self._closed = True
        if self._client:
            self._client.close()
        self._close_discovery()
