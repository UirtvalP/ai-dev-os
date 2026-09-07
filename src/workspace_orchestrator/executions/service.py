"""Execution 生命周期服务；Runtime Session 只作为 Execution 的可选引用。"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

from ..agent_runtime.contracts import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    EventSink,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from ..agent_runtime.events import RuntimeEventStore
from ..agent_runtime.ports import AgentRuntimePort
from .models import Execution, ExecutionStatus
from .store import ExecutionStore


class _DemoRuntime:
    """只供 CLI vertical slice 使用的确定性 Runtime。"""

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink

    def start(self, request: AgentRunRequest) -> RuntimeOperationResult:
        session = RuntimeSessionRef(
            "fake", f"fake-{uuid4().hex[:8]}", request.run_id,
            str(request.workspace_path), execution_id=request.execution_id,
        )
        self.sink(AgentEvent(
            str(uuid4()), request.run_id, "fake", "session.started",
            {"message": "fake runtime 已启动"}, session_id=session.session_id,
            turn_id="turn-1",
        ))
        return RuntimeOperationResult("ok", session=session, turn_id="turn-1")

    def wait(
        self, session: RuntimeSessionRef, turn_id: str, *, timeout_seconds: float
    ) -> AgentRunResult:
        self.sink(AgentEvent(
            str(uuid4()), session.run_id, "fake", "completion",
            {"message": "P0 vertical slice 可运行"}, session_id=session.session_id,
            turn_id=turn_id,
        ))
        return AgentRunResult(
            0, session.session_id, "P0 demo 完成", "", runtime_id="fake",
            run_id=session.run_id, summary="fake runtime demo 完成",
        )

    def close(self) -> None:
        return None


class ExecutionService:
    def __init__(self, executions: ExecutionStore, events: RuntimeEventStore) -> None:
        self.executions = executions
        self.events = events

    def demo(self, requirement_id: str, *, task_id: str = "TASK-DEMO") -> Execution:
        """通过正式 RuntimeExecutor 路径运行 fake runtime，不启动外部 Agent。"""

        # 局部导入避免 executions 包初始化时与 agent_runtime.execution 形成模块环。
        from ..agent_runtime.execution import RuntimeExecutor

        bridge = RuntimeExecutor(
            lambda sink: cast(AgentRuntimePort, _DemoRuntime(sink)), self.events,
            execution_store=self.executions, runtime_id="fake",
        )
        result = bridge.execute_tracked(
            requirement_id, task_id, self.executions.workspace.working_root,
            "输出 Execution 可追溯 demo", model="demo-model",
            reasoning_effort="low", source="demo",
        )
        assert result.run_id is not None
        return self.executions.get(result.run_id)

    def map_legacy_sessions(self, requirement_id: str) -> tuple[Execution, ...]:
        """把旧 Session 幂等映射为 Execution，不修改原 sessions.json。"""

        # 同一 Session 的多个 Task 必须作为一个幂等批次观察；否则并发调用可能在
        # create() 与 queued -> 最终状态 update() 之间读到中间态，并返回不同快照。
        with self.executions.workspace.locked():
            return self._map_legacy_sessions_locked(requirement_id)

    def _map_legacy_sessions_locked(self, requirement_id: str) -> tuple[Execution, ...]:
        data = self.executions.workspace.load(requirement_id)
        mapped: list[Execution] = []
        for session in data["sessions"]:
            session_id = str(session.get("id") or "").strip()
            if not session_id:
                continue
            runtime_id = str(session.get("agent") or data["meta"].get("agent_provider") or "legacy")
            raw_task_ids = session.get("task_ids")
            task_ids = (
                [str(item) for item in raw_task_ids if str(item).strip()]
                if isinstance(raw_task_ids, list)
                else []
            ) or ["LEGACY-SESSION"]
            for task_id in task_ids:
                creation_key = f"legacy:{requirement_id}:{session_id}:{task_id}"
                execution = self.executions.create(
                    requirement_id, task_id, role="implementation", runtime_id=runtime_id,
                    provider=runtime_id, prompt="由旧 Session 无损映射",
                    source="legacy-thread-binding", creation_key=creation_key,
                )
                result = str(session.get("result") or "")
                status = cast(ExecutionStatus, {
                    "in_progress": "running",
                    "completed": "completed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "blocked": "blocked",
                    "pending_auto_finish": "waiting",
                    "detached": "waiting",
                }.get(result, "waiting"))
                if execution.status == "queued":
                    execution = self.executions.update(
                        execution.id, status=status, session_id=session_id,
                        started_at=str(session.get("started_at") or execution.created_at),
                        completed_at=(
                            None if status in {"running", "waiting", "blocked"}
                            else str(session.get("ended_at") or execution.updated_at)
                        ),
                        last_progress_at=str(
                            session.get("ended_at") or session.get("started_at")
                            or execution.updated_at
                        ),
                        summary=f"legacy Session 映射：{result or 'unknown'}",
                        result={"legacy_session": dict(session)},
                    )
                mapped.append(execution)
        return tuple(mapped)
