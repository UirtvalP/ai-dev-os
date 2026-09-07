"""独立 Workbench 主动创建并启动 Execution。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from threading import RLock
from typing import Any, cast
from uuid import uuid4

from .agent_runtime.contracts import (
    AgentEvent,
    ExecutionSpec,
    ModelDescriptor,
    OperationStatus,
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
        self._pending_cleanup: list[StandardAgentRuntimePort] = []
        self._reply_lock = RLock()

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

    def reply_capability(self, requirement_id: str, execution_id: str) -> dict[str, object]:
        """报告能否继续原 Session；探测不会启动或恢复 Agent。"""

        execution = self._owned_execution(requirement_id, execution_id)
        runtime = self._runtimes.get(execution.id)
        created = runtime is None
        try:
            session = self._session_ref(execution)
            runtime = runtime or self.runtime_factory(execution.runtime_id, self.events)
            descriptor = runtime.describe()
            supported = (
                descriptor.available
                and descriptor.runtime_id == execution.runtime_id
                and descriptor.supports("resume")
                and descriptor.supports("interactive_message")
            )
            reason = descriptor.reason
            if descriptor.runtime_id != execution.runtime_id:
                reason = "Runtime descriptor 身份与 Execution 不一致"
            elif not descriptor.supports("resume"):
                reason = "Runtime 不支持恢复原 Session"
            elif not descriptor.supports("interactive_message"):
                reason = "Runtime 不支持向原 Session 发送消息"
            return {
                "supported": supported,
                "runtime_id": execution.runtime_id,
                "session_id": session.session_id,
                "capabilities": descriptor.canonical_capabilities,
                "reason": reason,
                "alternative": None if supported else "请在原生 Agent 中打开该 Session 继续；Workbench 不会新建 Agent。",
            }
        except Exception as exc:  # noqa: BLE001 -- Runtime 探测属于外部边界。
            return {
                "supported": False,
                "runtime_id": execution.runtime_id,
                "session_id": execution.session_id,
                "capabilities": (),
                "reason": str(exc),
                "alternative": "请在原生 Agent 中打开该 Session 继续；Workbench 不会新建 Agent。",
            }
        finally:
            if created and runtime is not None:
                self._close_or_retain(runtime)

    def reply(
        self, requirement_id: str, execution_id: str, message: str, *, command_id: str,
    ) -> RuntimeOperationResult:
        """继续 Execution 的原 Session；此路径永不调用 Runtime.start。"""

        with self._reply_lock:
            return self._reply_locked(
                requirement_id, execution_id, message, command_id=command_id,
            )

    def close(self) -> None:
        with self._reply_lock:
            self._close_locked()

    def _close_locked(self) -> None:
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
        for runtime in tuple(self._pending_cleanup):
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001 -- 保留引用供下次 close 重试。
                errors.append(f"临时 Runtime: {exc}")
            else:
                self._pending_cleanup.remove(runtime)
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

    def _owned_execution(self, requirement_id: str, execution_id: str) -> Execution:
        execution = self.executions.get(execution_id)
        if execution.requirement_id != requirement_id.upper():
            raise WorkspaceError("Execution 不属于当前 Requirement")
        return execution

    def _session_ref(self, execution: Execution) -> RuntimeSessionRef:
        raw = execution.result.get("session_ref")
        if not isinstance(raw, dict):
            raise WorkspaceError("Execution 没有可恢复的完整 SessionRef")
        names = {item.name for item in fields(RuntimeSessionRef)}
        try:
            session = RuntimeSessionRef(**{name: raw[name] for name in names if name in raw})
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(f"Execution SessionRef 损坏：{exc}") from exc
        expected = RuntimeSessionRef(
            execution.runtime_id, execution.session_id or "", execution.id,
            execution.workspace_path, execution_id=execution.id,
            sandbox=execution.execution_policy.get("sandbox"), model=execution.model,
            reasoning_effort=execution.reasoning_effort,
            requirement_id=execution.requirement_id, task_id=execution.task_id,
        )
        if session != expected or not session.session_id or not session.sandbox:
            raise WorkspaceError("Execution SessionRef 身份与持久化 Execution 不一致")
        return session

    def _reply_locked(
        self, requirement_id: str, execution_id: str, message: str, *, command_id: str,
    ) -> RuntimeOperationResult:
        execution = self._owned_execution(requirement_id, execution_id)
        session = self._session_ref(execution)
        replay = self._claim_reply(execution, message, command_id)
        if replay is not None:
            return replay
        runtime = self._runtimes.get(execution.id)
        created = runtime is None
        operation: RuntimeOperationResult
        try:
            runtime = runtime or self.runtime_factory(execution.runtime_id, self.events)
            descriptor = runtime.describe()
            if not descriptor.available:
                operation = self._reply_failure(
                    "unavailable", session, descriptor.reason or "Runtime 不可用",
                )
            elif descriptor.runtime_id != execution.runtime_id:
                operation = self._reply_failure(
                    "failed", session, "Runtime descriptor 身份与 Execution 不一致",
                    code="identity_mismatch",
                )
            elif created and not descriptor.supports("resume"):
                operation = self._reply_failure(
                    "unsupported", session, "Runtime 不支持恢复原 Session",
                )
            elif not descriptor.supports("interactive_message"):
                operation = self._reply_failure(
                    "unsupported", session, "Runtime 不支持向原 Session 发送消息",
                )
            else:
                operation = (
                    runtime.resume(session, message)
                    if created else runtime.send_message(session, message)
                )
            if operation.session is not None and operation.session != session:
                operation = self._reply_failure(
                    "failed", session, "Runtime 返回的 Session 身份与 Execution 不一致",
                    code="identity_mismatch",
                )
            elif operation.ok and (operation.session is None or not operation.turn_id):
                operation = self._reply_failure(
                    "failed", session, "Runtime 未返回完整 Session/Turn 身份",
                    code="identity_mismatch",
                )
            delivery = "resume" if created else "send_message"
            if not operation.ok:
                operation = replace(operation, data={
                    **operation.data,
                    "delivery": delivery,
                    "continued_original_session": False,
                    "alternative": "请在原生 Agent 中打开该 Session 继续；Workbench 不会新建 Agent。",
                })
                self._persist_reply(execution, message, command_id, "completed", operation)
                return operation
            self._runtimes[execution.id] = runtime
            operation = replace(operation, data={
                **operation.data,
                "delivery": delivery,
                "continued_original_session": True,
            })
            # Provider 已接收后先落 durable receipt；后续失败不得自动重复投递。
            self._persist_reply(execution, message, command_id, "delivered", operation)
            timestamp = now_iso()
            try:
                self.executions.update(
                    execution.id, status="running", turn_id=operation.turn_id,
                    completed_at=None, last_progress_at=timestamp,
                    result={
                        **execution.result,
                        "last_reply": {
                            "command_id": command_id, "delivery": delivery,
                            "turn_id": operation.turn_id, "at": timestamp,
                        },
                    },
                )
            except Exception as exc:  # noqa: BLE001 -- 已投递，必须报告未知而不是允许重试。
                unknown = self._reply_failure(
                    "failed", session,
                    f"消息已由 Provider 接收，但 Execution 落盘失败：{exc}",
                    code="execution_persist_failed",
                )
                unknown = replace(unknown, data={
                    **unknown.data, "delivery_accepted": True, "turn_id": operation.turn_id,
                })
                self._persist_reply(execution, message, command_id, "delivery_unknown", unknown)
                return unknown
            self._persist_reply(execution, message, command_id, "completed", operation)
            return operation
        except Exception as exc:  # noqa: BLE001 -- Runtime 是外部进程/协议边界。
            operation = self._reply_failure(
                "failed", session, str(exc), code="runtime_reply_failed",
            )
            self._persist_reply(execution, message, command_id, "completed", operation)
            return operation
        finally:
            if created and runtime is not None and execution.id not in self._runtimes:
                self._close_or_retain(runtime)

    def _claim_reply(
        self, execution: Execution, message: str, command_id: str,
    ) -> RuntimeOperationResult | None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", command_id) is None:
            raise WorkspaceError("command_id 格式无效")
        path = self._reply_path(execution, command_id)
        with self.workspace.locked():
            if path.exists():
                record = self.workspace.read_json(path)
                if (
                    record.get("execution_id") != execution.id
                    or record.get("message") != message
                ):
                    raise WorkspaceError("command_id 已绑定不同 Execution 回复")
                stored = record.get("operation")
                if isinstance(stored, dict):
                    replay = self._operation_from_dict(stored)
                    expected = self._session_ref(execution)
                    if replay.session is not None and replay.session != expected:
                        raise WorkspaceError("Execution reply receipt 的 Session 身份不匹配")
                    if replay.ok and (replay.session is None or not replay.turn_id):
                        raise WorkspaceError("Execution reply receipt 缺少完整 Session/Turn 身份")
                    return replace(replay, data={**replay.data, "idempotent_replay": True})
                return self._reply_failure(
                    "failed", self._session_ref(execution),
                    "相同 command_id 的投递仍在进行或结果未知；为避免重复发送不会自动重试",
                    code="reply_in_progress",
                )
            self.workspace.write_json(path, {
                "command_id": command_id, "execution_id": execution.id,
                "requirement_id": execution.requirement_id, "message": message,
                "state": "delivering", "updated_at": now_iso(),
            })
        return None

    def _persist_reply(
        self, execution: Execution, message: str, command_id: str, state: str,
        operation: RuntimeOperationResult,
    ) -> None:
        with self.workspace.locked():
            self.workspace.write_json(self._reply_path(execution, command_id), {
                "command_id": command_id, "execution_id": execution.id,
                "requirement_id": execution.requirement_id, "message": message,
                "state": state, "updated_at": now_iso(),
                "operation": self._operation_to_dict(operation),
            })

    def _reply_path(self, execution: Execution, command_id: str) -> Path:
        return (
            self.workspace.path_for(execution.requirement_id)
            / "executions" / "replies" / f"{command_id}.json"
        )

    @staticmethod
    def _operation_to_dict(operation: RuntimeOperationResult) -> dict[str, Any]:
        return {
            "status": operation.status,
            "session": asdict(operation.session) if operation.session else None,
            "turn_id": operation.turn_id,
            "data": operation.data,
            "error": operation.error.to_dict() if operation.error else None,
        }

    @staticmethod
    def _operation_from_dict(raw: dict[str, Any]) -> RuntimeOperationResult:
        session = RuntimeSessionRef(**raw["session"]) if raw.get("session") else None
        error = RuntimeFailure(**raw["error"]) if raw.get("error") else None
        return RuntimeOperationResult(
            cast(OperationStatus, raw["status"]), session=session, turn_id=raw.get("turn_id"),
            data=dict(raw.get("data", {})), error=error,
        )

    def _close_or_retain(self, runtime: StandardAgentRuntimePort) -> None:
        try:
            runtime.close()
        except Exception:  # noqa: BLE001 -- 保留强引用，交给显式 close 重试。
            self._pending_cleanup.append(runtime)

    @staticmethod
    def _reply_failure(
        status: OperationStatus, session: RuntimeSessionRef, message: str,
        *, code: str = "unsupported",
    ) -> RuntimeOperationResult:
        return RuntimeOperationResult(
            status, session=session,
            data={
                "continued_original_session": False,
                "alternative": "请在原生 Agent 中打开该 Session 继续；Workbench 不会新建 Agent。",
            },
            error=RuntimeFailure(code, message),
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
