"""Provider 无关的 Runtime Contract 门面。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .contracts import (
    AgentEvent,
    AgentRunRequest,
    ExecutionSpec,
    ModelDescriptor,
    RuntimeDescriptor,
    RuntimeFailure,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from .events import RuntimeEventStore
from .ports import AgentRuntimePort


class AgentRuntime:
    """把现有 Adapter 收敛到 Workbench 需要的稳定操作集合。"""

    def __init__(self, adapter: AgentRuntimePort, events: RuntimeEventStore) -> None:
        self._adapter = adapter
        self._events = events
        self._active_turns: dict[tuple[str, str], str] = {}

    def describe(self) -> RuntimeDescriptor:
        return self._adapter.describe()

    def list_models(self) -> tuple[ModelDescriptor, ...]:
        descriptor = self.describe()
        return descriptor.models if descriptor.available else ()

    def start(self, spec: ExecutionSpec) -> RuntimeOperationResult:
        return self._remember(self._adapter.start(spec.to_request()))

    def resume(self, session: RuntimeSessionRef, message: str) -> RuntimeOperationResult:
        if (
            not session.run_id or not session.execution_id
            or not session.workspace_path or not session.sandbox
        ):
            return RuntimeOperationResult(
                "failed", session=session,
                error=RuntimeFailure(
                    "incomplete_session_ref",
                    "恢复需要包含 run_id、execution_id、workspace_path 与 sandbox 的完整 Session 引用",
                ),
            )
        request = AgentRunRequest(
            run_id=session.run_id,
            workspace_path=self._workspace_path(session),
            prompt=message,
            sandbox=session.sandbox,
            model=session.model,
            resume_session_id=session.session_id,
            reasoning_effort=session.reasoning_effort,
            execution_id=session.execution_id,
            requirement_id=session.requirement_id,
            task_id=session.task_id,
        )
        return self._remember(self._adapter.resume(request))

    def send_message(self, session: RuntimeSessionRef, message: str) -> RuntimeOperationResult:
        return self._remember(self._adapter.send_message(session, message))

    def cancel(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        turn_id = self._active_turns.get(self._key(session))
        if turn_id and self._turn_completed(session, turn_id, {}):
            self._active_turns.pop(self._key(session), None)
            turn_id = None
        if not turn_id:
            return RuntimeOperationResult(
                "unsupported", session=session,
                error=RuntimeFailure("unknown_active_turn", "没有可取消的活动轮次"),
            )
        result = self._adapter.interrupt(session, turn_id)
        return result

    def status(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        key = self._key(session)
        turn_id = self._active_turns.get(key)
        if turn_id and self._turn_completed(session, turn_id, {}):
            self._active_turns.pop(key, None)
            turn_id = None
        result = self._adapter.read_session(session)
        if not result.ok:
            return result
        if turn_id and self._turn_completed(session, turn_id, result.data):
            self._active_turns.pop(key, None)
            turn_id = None
        data = dict(result.data)
        data.setdefault("active_turn_id", turn_id)
        return replace(result, data=data)

    def list_events(
        self, session: RuntimeSessionRef, *, after: int = 0, limit: int = 1000,
    ) -> tuple[AgentEvent, ...]:
        return self._events.query(
            runtime_id=session.runtime_id,
            session_id=session.session_id,
            after=after,
            limit=limit,
        )

    def stream_events(
        self, session: RuntimeSessionRef, *, after: int = 0, limit: int = 1000,
    ) -> tuple[AgentEvent, ...]:
        """返回当前可消费事件页；调用方用 sequence 游标继续拉取。"""

        return self.list_events(session, after=after, limit=limit)

    def archive(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        result = self._adapter.archive(session)
        if result.ok:
            self._active_turns.pop(self._key(session), None)
        return result

    def close(self) -> None:
        try:
            self._adapter.close()
        finally:
            self._active_turns.clear()

    def _remember(self, result: RuntimeOperationResult) -> RuntimeOperationResult:
        if result.ok and result.session and result.turn_id:
            self._active_turns[self._key(result.session)] = result.turn_id
        return result

    @staticmethod
    def _key(session: RuntimeSessionRef) -> tuple[str, str]:
        return session.runtime_id, session.session_id

    @staticmethod
    def _workspace_path(session: RuntimeSessionRef) -> Path:
        return Path(session.workspace_path)

    def _turn_completed(
        self, session: RuntimeSessionRef, turn_id: str, status: dict[str, object],
    ) -> bool:
        terminal = {"completed", "failed", "cancelled", "canceled", "archived", "closed"}
        direct = status.get("state", status.get("status"))
        if isinstance(direct, str) and direct.lower() in terminal:
            return True
        thread = status.get("thread")
        if isinstance(thread, dict):
            turns = thread.get("turns")
            if isinstance(turns, list):
                for turn in reversed(turns):
                    if not isinstance(turn, dict) or turn.get("id") != turn_id:
                        continue
                    state = turn.get("status", turn.get("state"))
                    return isinstance(state, str) and state.lower() in terminal
        return any(
            event.turn_id == turn_id and event.kind == "completion"
            for event in self.list_events(session)
        )
