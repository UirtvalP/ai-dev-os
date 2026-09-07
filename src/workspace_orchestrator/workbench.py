"""独立 Workbench 主动创建并启动 Execution。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from .agent_runtime.contracts import (
    AgentEvent,
    ExecutionSpec,
    ModelDescriptor,
    RuntimeDescriptor,
    RuntimeFailure,
    RuntimeOperationResult,
    RuntimeSessionRef,
)
from .agent_runtime.events import RuntimeEventStore
from .agent_runtime.ports import StandardAgentRuntimePort
from .executions import Execution, ExecutionStore
from .workspace import WorkspaceError, WorkspaceStore, now_iso

RuntimeFactory = Callable[[str, RuntimeEventStore], StandardAgentRuntimePort]


@dataclass(frozen=True, slots=True)
class WorkbenchStart:
    requirement_id: str
    task_id: str
    message: str
    runtime_id: str
    role: str = "implementation"
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str = "workspace-write"
    creation_key: str | None = None


class WorkbenchExecutionService:
    """由 AI Dev OS 持有启动顺序；原生 Agent Hook 不参与。"""

    def __init__(self, workspace: WorkspaceStore, runtime_factory: RuntimeFactory) -> None:
        self.workspace = workspace
        self.executions = ExecutionStore(workspace)
        self.events = RuntimeEventStore(workspace.root / "runtime-events")
        self.runtime_factory = runtime_factory
        self._runtimes: dict[str, StandardAgentRuntimePort] = {}

    def start(self, request: WorkbenchStart) -> Execution:
        execution = self.executions.create(
            request.requirement_id, request.task_id, role=request.role,
            runtime_id=request.runtime_id, model=request.model,
            reasoning_effort=request.reasoning_effort, prompt=request.message,
            workspace_path=self.workspace.working_root, source="workbench",
            creation_key=request.creation_key,
            execution_policy={"sandbox": request.sandbox},
        )
        self._require_same_request(execution, request)
        if execution.status != "queued":
            return execution
        prepared, claimed = self.executions.claim_start(
            execution.id, role=request.role, runtime_id=request.runtime_id,
            provider=request.runtime_id, model=request.model,
            reasoning_effort=request.reasoning_effort,
            workspace_path=self.workspace.working_root, prompt=request.message,
        )
        if not claimed:
            return prepared
        runtime: StandardAgentRuntimePort | None = None
        try:
            runtime = self.runtime_factory(request.runtime_id, self.events)
            descriptor = runtime.describe()
            failure = self._validate_route(request, descriptor)
            if failure:
                runtime.close()
                return self._fail(execution, failure)
            if descriptor.runtime_id != request.runtime_id:
                return self._close_and_fail(prepared, runtime, RuntimeFailure(
                    "identity_mismatch", "Runtime descriptor 身份与请求不一致",
                ))
            operation = runtime.start(ExecutionSpec(
                run_id=prepared.id, workspace_path=Path(prepared.workspace_path),
                message=prepared.prompt, execution_id=prepared.id,
                sandbox=request.sandbox, model=request.model,
                reasoning_effort=request.reasoning_effort,
                requirement_id=prepared.requirement_id, task_id=prepared.task_id,
            ))
        except Exception as exc:  # noqa: BLE001 -- Runtime 是外部进程/协议边界。
            if runtime is not None:
                try:
                    runtime.close()
                except Exception as close_exc:  # noqa: BLE001
                    return self._fail(execution, RuntimeFailure(
                        "runtime_cleanup_unknown",
                        f"Runtime 启动失败且清理未确认：{exc}；{close_exc}",
                    ))
            return self._fail(execution, RuntimeFailure("runtime_start_failed", str(exc)))
        if not operation.ok or not operation.session or not operation.turn_id:
            error = operation.error or RuntimeFailure(
                "protocol_error", "Runtime.start 未返回完整 Session/Turn 身份",
            )
            return self._close_and_fail(prepared, runtime, error)
        session = operation.session
        if (
            not session.workspace_path
            or session.runtime_id != descriptor.runtime_id
            or session.execution_id != prepared.id
            or session.run_id != prepared.id
            or session.requirement_id != prepared.requirement_id
            or session.task_id != prepared.task_id
            or Path(session.workspace_path).resolve() != Path(prepared.workspace_path).resolve()
            or session.sandbox != request.sandbox
            or session.model != request.model
            or session.reasoning_effort != request.reasoning_effort
        ):
            return self._close_and_fail(prepared, runtime, RuntimeFailure(
                "identity_mismatch", "Runtime.start 返回的身份与 Execution 不一致",
            ))
        timestamp = now_iso()
        try:
            running = self.executions.update(
                prepared.id, status="running", session_id=session.session_id,
                turn_id=operation.turn_id, started_at=timestamp, last_progress_at=timestamp,
                result={
                    "runtime": descriptor.runtime_id,
                    "operation": operation.data,
                    "session_ref": asdict(session),
                },
            )
        except Exception as exc:  # noqa: BLE001 -- 落盘失败不能遗留无人持有的 Runtime。
            return self._close_and_fail(prepared, runtime, RuntimeFailure(
                "execution_persist_failed", f"Runtime 已启动但 Execution 落盘失败：{exc}",
            ))
        self._runtimes[prepared.id] = runtime
        return running

    def close(self) -> None:
        errors: list[str] = []
        for execution_id, runtime in tuple(self._runtimes.items()):
            try:
                runtime.close()
                execution = self.executions.get(execution_id)
                if execution.status in {"starting", "running"}:
                    self.executions.update(
                        execution_id, status="waiting", last_progress_at=now_iso(),
                        summary="Workbench 服务已关闭；保留 SessionRef 等待安全恢复",
                    )
            except Exception as exc:  # noqa: BLE001 -- 保留引用供调用方重试清理。
                errors.append(f"{execution_id}: {exc}")
            else:
                self._runtimes.pop(execution_id, None)
        if errors:
            raise WorkspaceError("Runtime 清理未确认：" + "；".join(errors))

    def _validate_route(
        self, request: WorkbenchStart, descriptor: RuntimeDescriptor,
    ) -> RuntimeFailure | None:
        if not descriptor.available:
            return RuntimeFailure("runtime_unavailable", descriptor.reason or "Runtime 不可用")
        if not descriptor.supports("start"):
            return RuntimeFailure("unsupported", "Runtime 不支持 start")
        if request.model and request.model not in {item.id for item in descriptor.models}:
            return RuntimeFailure("unsupported_model", f"Runtime 未发现模型：{request.model}")
        if request.reasoning_effort:
            model = next(
                (item for item in descriptor.models if item.id == request.model),
                next((item for item in descriptor.models if item.is_default), None),
            )
            if model is None or request.reasoning_effort not in model.reasoning_efforts:
                return RuntimeFailure(
                    "unsupported_reasoning", f"Runtime 未发现 reasoning：{request.reasoning_effort}",
                )
        return None

    def _fail(self, execution: Execution, failure: RuntimeFailure) -> Execution:
        return self.executions.update(
            execution.id, status="failed", completed_at=now_iso(),
            last_progress_at=now_iso(), error=failure.to_dict(), summary=failure.message,
        )

    def _close_and_fail(
        self, execution: Execution, runtime: StandardAgentRuntimePort,
        failure: RuntimeFailure,
    ) -> Execution:
        try:
            runtime.close()
        except Exception as exc:  # noqa: BLE001 -- 未确认清理必须进入持久诊断。
            self._runtimes[execution.id] = runtime
            failure = RuntimeFailure(
                "runtime_cleanup_unknown",
                f"{failure.message}；Runtime 清理未确认：{exc}",
                details={"cause": failure.to_dict()},
            )
        return self._fail(execution, failure)

    def _require_same_request(self, execution: Execution, request: WorkbenchStart) -> None:
        expected = {
            "requirement_id": request.requirement_id.upper(),
            "task_id": request.task_id,
            "role": request.role,
            "runtime_id": request.runtime_id,
            "model": request.model,
            "reasoning_effort": request.reasoning_effort,
            "workspace_path": str(self.workspace.working_root.resolve()),
            "prompt": request.message,
            "sandbox": request.sandbox,
        }
        actual = {
            "requirement_id": execution.requirement_id,
            "task_id": execution.task_id,
            "role": execution.role,
            "runtime_id": execution.runtime_id,
            "model": execution.model,
            "reasoning_effort": execution.reasoning_effort,
            "workspace_path": execution.workspace_path,
            "prompt": execution.prompt,
            "sandbox": execution.execution_policy.get("sandbox"),
        }
        if actual != expected:
            raise WorkspaceError(
                f"Workbench creation_key 已绑定不同启动请求：{request.creation_key}"
            )


class DemoWorkbenchRuntime:
    """P2 可见 vertical slice；不启动或修改任何原生 Agent。"""

    def __init__(self, events: RuntimeEventStore) -> None:
        self.events = events
        self.closed = False

    def describe(self) -> RuntimeDescriptor:
        return RuntimeDescriptor("workbench-demo", "Workbench Demo", "1", True, ("start",))

    def list_models(self) -> tuple[ModelDescriptor, ...]:
        return ()

    def start(self, spec: ExecutionSpec) -> RuntimeOperationResult:
        session = RuntimeSessionRef(
            "workbench-demo", f"demo-{uuid4().hex[:8]}", spec.run_id,
            str(spec.workspace_path), execution_id=spec.execution_id,
            sandbox=spec.sandbox, model=spec.model, reasoning_effort=spec.reasoning_effort,
            requirement_id=spec.requirement_id, task_id=spec.task_id,
        )
        self.events.append(AgentEvent(
            str(uuid4()), spec.run_id, "workbench-demo", "session",
            {"detail": "Workbench 主动启动"}, session_id=session.session_id,
            turn_id="turn-demo", requirement_id=spec.requirement_id,
            task_id=spec.task_id, execution_id=spec.execution_id,
        ))
        return RuntimeOperationResult("ok", session, "turn-demo", {"demo": True})

    def resume(
        self, session: RuntimeSessionRef, message: str,
    ) -> RuntimeOperationResult:
        return RuntimeOperationResult("unsupported", session)

    def send_message(
        self, session: RuntimeSessionRef, message: str,
    ) -> RuntimeOperationResult:
        return RuntimeOperationResult("unsupported", session)

    def cancel(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return RuntimeOperationResult("unsupported", session)

    def status(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return RuntimeOperationResult("ok", session, data={"state": "running"})

    def list_events(
        self, session: RuntimeSessionRef, *, after: int = 0, limit: int = 1000,
    ) -> tuple[AgentEvent, ...]:
        return self.events.query(session_id=session.session_id, after=after, limit=limit)

    def stream_events(
        self, session: RuntimeSessionRef, *, after: int = 0, limit: int = 1000,
    ) -> tuple[AgentEvent, ...]:
        return self.list_events(session, after=after, limit=limit)

    def archive(self, session: RuntimeSessionRef) -> RuntimeOperationResult:
        return RuntimeOperationResult("unsupported", session)

    def close(self) -> None:
        self.closed = True


def demo_workbench(workspace: WorkspaceStore, requirement_id: str, task_id: str) -> Execution:
    service = WorkbenchExecutionService(
        workspace, lambda _name, events: DemoWorkbenchRuntime(events),
    )
    try:
        execution = service.start(WorkbenchStart(
            requirement_id, task_id, "P2 Workbench 主动启动 Demo", "workbench-demo",
            creation_key=f"p2-demo:{requirement_id}:{task_id}",
        ))
        if execution.status != "running":
            return execution
        timestamp = now_iso()
        service.events.append(AgentEvent(
            str(uuid4()), execution.id, execution.runtime_id, "completion",
            {"detail": "P2 Demo 已完成"}, session_id=execution.session_id,
            turn_id=execution.turn_id, requirement_id=execution.requirement_id,
            task_id=execution.task_id, execution_id=execution.id,
        ))
        return service.executions.update(
            execution.id, status="completed", completed_at=timestamp,
            last_progress_at=timestamp, summary="P2 Workbench vertical slice 完成",
            result={**execution.result, "demo": True, "authoritative_runtime": False},
        )
    finally:
        service.close()
