"""执行端口与交互 Runtime 端口；所有绑定由既有 Hook 生命周期负责。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    AgentRunRequest,
    AgentRunResult,
    RuntimeDescriptor,
    RuntimeOperationResult,
    RuntimeSessionRef,
)


class AgentExecutionPort(Protocol):
    def execute(
        self,
        workspace_path: Path,
        prompt: str,
        *,
        sandbox: str = "workspace-write",
        model: str | None = None,
        resume_session_id: str | None = None,
        bypass_hook_trust: bool = False,
    ) -> AgentRunResult: ...


class AgentRuntimePort(Protocol):
    def describe(self) -> RuntimeDescriptor: ...

    # start/resume 启动 request.prompt 对应的首轮，返回实际 session/turn 引用。
    def start(self, request: AgentRunRequest) -> RuntimeOperationResult: ...
    def resume(self, request: AgentRunRequest) -> RuntimeOperationResult: ...
    def read_session(self, session: RuntimeSessionRef) -> RuntimeOperationResult: ...
    def send_message(self, session: RuntimeSessionRef, text: str) -> RuntimeOperationResult: ...
    def steer(
        self, session: RuntimeSessionRef, turn_id: str, text: str
    ) -> RuntimeOperationResult: ...
    def interrupt(self, session: RuntimeSessionRef, turn_id: str) -> RuntimeOperationResult: ...
    def archive(self, session: RuntimeSessionRef) -> RuntimeOperationResult: ...
    def respond_to_request(
        self, session: RuntimeSessionRef, request_id: str | int, decision: dict[str, Any]
    ) -> RuntimeOperationResult: ...
    def wait(
        self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float
    ) -> AgentRunResult: ...
    def close(self) -> None: ...
