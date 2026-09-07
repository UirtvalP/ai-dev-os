"""将交互 Runtime 适配为既有 Dispatcher 的同步执行端口。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from ..executions.models import ExecutionStatus
from ..executions.store import ExecutionStore
from ..workspace import now_iso
from .contracts import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    EventSink,
    RuntimeFailure,
    RuntimeOperationResult,
)
from .events import RuntimeEventStore
from .ports import AgentRuntimePort


@dataclass(slots=True)
class RuntimeExecutor:
    runtime_factory: Callable[[EventSink], AgentRuntimePort]
    event_store: RuntimeEventStore
    timeout_seconds: float = 7200
    allow_managed_hook_trust: bool = False
    execution_store: ExecutionStore | None = None
    runtime_id: str = "configured"

    def execute(
        self,
        workspace_path: Path,
        prompt: str,
        *,
        sandbox: str = "workspace-write",
        model: str | None = None,
        resume_session_id: str | None = None,
        bypass_hook_trust: bool = False,
        reasoning_effort: str | None = None,
        requirement_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
    ) -> AgentRunResult:
        """仅在明确 Session 不存在时回退；超时和未知结果绝不重放输入。"""

        if (isinstance(self.timeout_seconds, bool)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("Runtime timeout_seconds 必须是有限正数")
        request = AgentRunRequest(
            run_id=execution_id or str(uuid4()),
            workspace_path=workspace_path.resolve(),
            prompt=prompt,
            sandbox=sandbox,
            model=model,
            resume_session_id=resume_session_id,
            # 托管 Hook 检查来自兼容 Dispatcher；只向明确支持的 Adapter 传递授权。
            bypass_hook_trust=bypass_hook_trust and self.allow_managed_hook_trust,
            timeout_seconds=self.timeout_seconds,
            reasoning_effort=reasoning_effort,
            requirement_id=requirement_id,
            task_id=task_id,
            execution_id=execution_id,
        )

        def persist(event: AgentEvent) -> None:
            if event.run_id != request.run_id:
                raise ValueError("Runtime 事件不能写入其他 run_id")
            scopes = {
                "requirement_id": request.requirement_id,
                "task_id": request.task_id,
                "execution_id": request.execution_id,
            }
            for name, expected in scopes.items():
                actual = getattr(event, name)
                if actual not in {None, expected}:
                    raise ValueError(f"Runtime 事件不能写入其他 {name}")
            self.event_store.append(replace(
                event,
                requirement_id=request.requirement_id,
                task_id=request.task_id,
                execution_id=request.execution_id,
            ))

        runtime: AgentRuntimePort | None = None
        operation: RuntimeOperationResult | None = None
        resumed = bool(resume_session_id)
        started = time.monotonic()
        result = AgentRunResult(1, None, "", "Runtime 未返回结果", run_id=request.run_id)
        try:
            runtime = self.runtime_factory(persist)
            operation = runtime.resume(request) if resume_session_id else runtime.start(request)
            if (
                resume_session_id
                and not operation.ok
                and operation.error is not None
                and operation.error.code == "session_missing"
            ):
                operation = runtime.start(replace(request, resume_session_id=None))
                resumed = False
            if not operation.ok:
                result = self._failed(operation, request.run_id, resumed)
            elif operation.session is None or not operation.turn_id:
                result = self._failed(
                    RuntimeOperationResult(
                        "failed", error=RuntimeFailure("protocol_error", "Runtime 缺少 Session/Turn 引用")
                    ), request.run_id, resumed,
                )
            elif operation.session.run_id != request.run_id:
                result = self._failed(
                    RuntimeOperationResult(
                        "failed", error=RuntimeFailure("scope_mismatch", "Runtime 返回了其他 run_id")
                    ), request.run_id, resumed,
                )
            else:
                if self.execution_store is not None and request.execution_id is not None:
                    progress = now_iso()
                    self.execution_store.update(
                        request.execution_id, status="running",
                        session_id=operation.session.session_id,
                        turn_id=operation.turn_id,
                        last_progress_at=progress,
                    )
                # 一旦外部 Session 已创建，即使 wait/清理失败也保留恢复身份。
                result = AgentRunResult(
                    1, operation.session.session_id, "", "Runtime 轮次尚未结束",
                    resumed=resumed, runtime_id=operation.session.runtime_id,
                    run_id=request.run_id,
                )
                remaining = self.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    runtime.interrupt(operation.session, operation.turn_id)
                    result = self._failed(
                        RuntimeOperationResult(
                            "timeout", session=operation.session,
                            error=RuntimeFailure("timeout", "Runtime 启动已超过运行时限"),
                        ), request.run_id, resumed,
                    )
                else:
                    result = runtime.wait(
                        operation.session, operation.turn_id, timeout_seconds=remaining
                    )
                    if (
                        result.session_id != operation.session.session_id
                        or result.run_id not in {None, request.run_id}
                    ):
                        result = self._failed(
                            RuntimeOperationResult(
                                "failed", error=RuntimeFailure("scope_mismatch", "Runtime 结果引用不匹配")
                            ), request.run_id, resumed,
                        )
                    else:
                        result = replace(result, run_id=request.run_id, resumed=resumed)
        except Exception as exc:  # noqa: BLE001 -- 外部可替换 Adapter 的故障边界。
            # 外部 Adapter 故障仍需返回结果，让既有 Dispatcher 进入可恢复阻塞态。
            surviving_session = operation.session if operation is not None else None
            result = replace(
                result, returncode=1,
                session_id=result.session_id or (
                    surviving_session.session_id if surviving_session else None
                ),
                runtime_id=(
                    surviving_session.runtime_id if surviving_session else result.runtime_id
                ),
                stderr=str(exc),
                error=RuntimeFailure("runtime_failure", str(exc)),
            )
        finally:
            try:
                if runtime is not None:
                    runtime.close()
            except Exception as exc:  # noqa: BLE001 -- 清理失败不得掩盖存活进程。
                result = replace(
                    result, returncode=1, stderr=f"Runtime 清理失败：{exc}",
                    error=RuntimeFailure("cleanup_failed", str(exc)),
                )
        return result

    def execute_tracked(
        self,
        requirement_id: str,
        task_id: str,
        workspace_path: Path,
        prompt: str,
        *,
        role: str = "implementation",
        sandbox: str = "workspace-write",
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume_session_id: str | None = None,
        bypass_hook_trust: bool = False,
        source: str = "dispatcher",
        execution_id: str | None = None,
    ) -> AgentRunResult:
        """先持久化 Execution，再允许 Runtime 创建 Session。"""

        if self.execution_store is None:
            raise ValueError("受追踪执行需要配置 ExecutionStore")
        if execution_id:
            execution = self.execution_store.get(execution_id)
            if (execution.requirement_id, execution.task_id) != (requirement_id, task_id):
                raise ValueError("queued Execution 与 Dispatcher 的 Requirement/Task 不匹配")
            if execution.status not in {"queued", "starting", "waiting"}:
                raise ValueError(f"Execution {execution.id} 当前不能启动：{execution.status}")
            execution = self.execution_store.prepare(
                execution.id, role=role, runtime_id=self.runtime_id,
                provider=self.runtime_id, model=model,
                reasoning_effort=reasoning_effort, workspace_path=workspace_path,
                prompt=prompt,
            )
        else:
            execution = self.execution_store.create(
                requirement_id, task_id, role=role, runtime_id=self.runtime_id,
                provider=self.runtime_id, model=model, reasoning_effort=reasoning_effort,
                prompt=prompt, workspace_path=workspace_path, source=source,
            )
        started = now_iso()
        self.execution_store.update(
            execution.id, status="starting", started_at=started, last_progress_at=started,
        )
        result = self.execute(
            workspace_path, prompt, sandbox=sandbox, model=model,
            reasoning_effort=reasoning_effort, resume_session_id=resume_session_id,
            bypass_hook_trust=bypass_hook_trust, requirement_id=requirement_id,
            task_id=task_id, execution_id=execution.id,
        )
        completed = now_iso()
        status: ExecutionStatus = "completed" if result.returncode == 0 else "failed"
        error = result.error.to_dict() if result.error else (
            {"code": "runtime_failed", "message": result.stderr} if result.returncode else None
        )
        persisted_events = self.event_store.query(
            execution_id=execution.id, limit=2_147_483_647
        )
        try:
            self.execution_store.update(
                execution.id, status=status, session_id=result.session_id,
                completed_at=completed, last_progress_at=completed,
                summary=result.summary, error=error,
                result={"returncode": result.returncode, "resumed": result.resumed},
                event_cursor=(persisted_events[-1].sequence if persisted_events else 0),
            )
        except Exception as exc:  # noqa: BLE001 -- 必须把已创建 Session 身份返回给调用方。
            return replace(
                result, returncode=1,
                stderr=f"Execution 最终状态持久化失败：{exc}",
                error=RuntimeFailure("execution_persistence_failed", str(exc)),
            )
        return result

    @staticmethod
    def _failed(
        operation: RuntimeOperationResult, run_id: str, resumed: bool
    ) -> AgentRunResult:
        error = operation.error or RuntimeFailure(operation.status, "Runtime 操作失败")
        return AgentRunResult(
            returncode={"unavailable": 127, "unsupported": 64, "timeout": 124}.get(
                operation.status, 1
            ),
            session_id=operation.session.session_id if operation.session else None,
            stdout="",
            stderr=error.message,
            resumed=resumed,
            runtime_id=operation.session.runtime_id if operation.session else "unknown",
            run_id=run_id,
            summary=error.message,
            error=error,
        )
