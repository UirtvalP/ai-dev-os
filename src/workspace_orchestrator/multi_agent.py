"""复用现有 Supervisor/Integration 的一等 Execution 追踪适配层。"""

from __future__ import annotations

import copy
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, cast

from .agent_runtime.contracts import AgentEvent, RuntimeSessionRef
from .agent_runtime.events import RuntimeEventStore
from .executions import Execution, ExecutionStatus, ExecutionStore
from .integration.contracts import MergeReceipt
from .orchestration.contracts import ModelRoute, TaskSpec, WorkerIsolation, WorkerObservation
from .orchestration.ports import WorkerExecutionPort
from .workspace import WorkspaceError, WorkspaceStore, now_iso


class IntegrationPort(Protocol):
    def __call__(self) -> dict[str, Any]: ...


class ExecutionTrackedWorkerPort:
    """把既有可信 Worker attempt 投影为一等 Execution，不改变其执行权威。"""

    def __init__(
        self, workspace: WorkspaceStore, requirement_id: str,
        delegate: WorkerExecutionPort, *, source_events: RuntimeEventStore | None = None,
    ) -> None:
        self.workspace = workspace
        self.requirement_id = requirement_id.upper()
        self.delegate = delegate
        self.executions = ExecutionStore(workspace)
        self.source_events = source_events
        self.events = RuntimeEventStore(workspace.root / "runtime-events")
        self._lock = RLock()

    def isolation(self, task: TaskSpec) -> WorkerIsolation:
        return self.delegate.isolation(task)

    def dispatch(
        self, attempt_id: str, fence: int, task: TaskSpec, route: ModelRoute,
    ) -> WorkerObservation:
        with self._lock:
            execution = self._prepare(attempt_id, fence, task, route)
            try:
                observation = self.delegate.dispatch(attempt_id, fence, task, route)
            except Exception as exc:
                self.executions.update(
                    execution.id, status="waiting", last_progress_at=now_iso(),
                    summary="Worker dispatch 结果未知；保留 Execution 防止重复启动",
                    error={"code": "ambiguous_dispatch", "message": str(exc)},
                )
                raise
            return self._sync(execution, attempt_id, fence, task, route, observation)

    def poll(self, attempt_id: str, fence: int) -> WorkerObservation:
        return self._observe("poll", attempt_id, fence)

    def reconcile(self, attempt_id: str, fence: int) -> WorkerObservation:
        return self._observe("reconcile", attempt_id, fence)

    def cancel(self, attempt_id: str, fence: int) -> WorkerObservation:
        return self._observe("cancel", attempt_id, fence)

    def _observe(self, method: str, attempt_id: str, fence: int) -> WorkerObservation:
        with self._lock:
            execution, task, route = self._bound(attempt_id, fence)
            operation = getattr(self.delegate, method)
            try:
                observation = operation(attempt_id, fence)
                return self._sync(
                    execution, attempt_id, fence, task, route, observation,
                )
            except Exception as exc:
                self.executions.update(
                    execution.id, status="waiting", last_progress_at=now_iso(),
                    summary=f"Worker {method}/sync 结果未知；等待 Supervisor reconcile",
                    error={"code": f"ambiguous_{method}", "message": str(exc)},
                )
                raise

    def _prepare(
        self, attempt_id: str, fence: int, task: TaskSpec, route: ModelRoute,
    ) -> Execution:
        if not task.worktree:
            raise WorkspaceError("Worker Task 必须绑定明确 worktree")
        execution = self.executions.create(
            self.requirement_id, task.task_id, role="worker", runtime_id=route.runtime_id,
            model=route.model, reasoning_effort=route.effort, prompt=task.prompt,
            workspace_path=Path(task.worktree), source="supervisor",
            branch=task.branch, worktree=task.worktree,
            creation_key=f"supervisor-attempt:{self.requirement_id}:{attempt_id}",
            execution_policy={
                "sandbox": route.sandbox, "attempt_id": attempt_id, "fence": fence,
                "task_spec": task.to_dict(), "route": route.to_dict(),
            },
        )
        expected = (
            execution.requirement_id, execution.task_id, execution.runtime_id,
            execution.model, execution.reasoning_effort,
            execution.execution_policy.get("attempt_id"), execution.execution_policy.get("fence"),
        )
        actual = (
            self.requirement_id, task.task_id, route.runtime_id, route.model, route.effort,
            attempt_id, fence,
        )
        expected_policy = {
            "sandbox": route.sandbox, "attempt_id": attempt_id, "fence": fence,
            "task_spec": task.to_dict(), "route": route.to_dict(),
        }
        if (
            expected != actual or execution.execution_policy != expected_policy
            or execution.branch != task.branch or execution.worktree != task.worktree
            or Path(execution.workspace_path).resolve() != Path(task.worktree).resolve()
        ):
            raise WorkspaceError("Worker attempt 已绑定不同 Execution 身份")
        if execution.status == "queued":
            execution, claimed = self.executions.claim_start(
                execution.id, role="worker", runtime_id=route.runtime_id,
                provider=route.runtime_id, model=route.model,
                reasoning_effort=route.effort, workspace_path=Path(task.worktree),
                prompt=task.prompt,
            )
            if not claimed:
                raise WorkspaceError("Worker Execution 未能取得唯一启动 claim")
            execution = self.executions.update(
                execution.id, status="starting", branch=task.branch, worktree=task.worktree,
                result={
                    "attempt_id": attempt_id, "fence": fence,
                    "task_spec": task.to_dict(), "route": route.to_dict(),
                },
            )
        return execution

    def _bound(self, attempt_id: str, fence: int) -> tuple[Execution, TaskSpec, ModelRoute]:
        matches = [
            item for item in self.executions.list(self.requirement_id)
            if item.creation_key == f"supervisor-attempt:{self.requirement_id}:{attempt_id}"
        ]
        if len(matches) != 1:
            raise WorkspaceError("Worker attempt 没有唯一的一等 Execution")
        execution = matches[0]
        try:
            task = TaskSpec.from_dict(execution.execution_policy["task_spec"])
            route = ModelRoute.from_dict(execution.execution_policy["route"])
        except (KeyError, TypeError, ValueError) as exc:
            self._invalidate_identity(execution, str(exc))
            raise WorkspaceError("Worker Execution 持久身份无效") from exc
        expected_policy = {
            "sandbox": route.sandbox, "attempt_id": attempt_id, "fence": fence,
            "task_spec": task.to_dict(), "route": route.to_dict(),
        }
        if (
            execution.execution_policy != expected_policy
            or execution.requirement_id != self.requirement_id
            or execution.task_id != task.task_id
            or execution.role != "worker"
            or execution.runtime_id != route.runtime_id
            or execution.provider != route.runtime_id
            or execution.model != route.model
            or execution.reasoning_effort != route.effort
            or execution.prompt != task.prompt
            or execution.branch != task.branch
            or execution.worktree != task.worktree
            or task.worktree is None
            or Path(execution.workspace_path).resolve() != Path(task.worktree).resolve()
        ):
            self._invalidate_identity(execution, "顶层字段与冻结 policy 不一致")
            raise WorkspaceError("Worker attempt 恢复身份与 Execution 不一致")
        return execution, task, route

    def _invalidate_identity(self, execution: Execution, message: str) -> None:
        self.executions.update(
            execution.id, status="waiting", last_progress_at=now_iso(),
            summary="Worker Execution 身份校验失败；禁止恢复",
            error={"code": "invalid_execution_identity", "message": message},
        )

    def _sync(
        self, execution: Execution, attempt_id: str, fence: int,
        task: TaskSpec, route: ModelRoute, observation: WorkerObservation,
    ) -> WorkerObservation:
        observation.validate()
        if (observation.attempt_id, observation.fence) != (attempt_id, fence):
            raise WorkspaceError("Worker observation 身份与 Execution 不一致")
        if execution.session_id is not None and observation.session_id != execution.session_id:
            raise WorkspaceError("Worker observation Session 与既有 Execution 不一致")
        cursor = self._sync_events(
            execution, attempt_id, task, route, observation.session_id,
        )
        status = cast(ExecutionStatus, {
            "running": "running", "candidate_complete": "completed",
            "blocked": "blocked", "failed": "failed", "unknown": "waiting",
        }[observation.state])
        completed_at = now_iso() if status in {"completed", "failed", "cancelled"} else None
        self.executions.update(
            execution.id, status=status, session_id=observation.session_id,
            started_at=execution.started_at or now_iso(), completed_at=completed_at,
            last_progress_at=now_iso(), event_cursor=cursor,
            summary=observation.summary,
            result={
                **execution.result, "observation": observation.to_dict(),
                **({"session_ref": asdict(RuntimeSessionRef(
                    route.runtime_id, observation.session_id, execution.id,
                    execution.workspace_path, execution_id=execution.id,
                    sandbox=route.sandbox, model=route.model,
                    reasoning_effort=route.effort, requirement_id=self.requirement_id,
                    task_id=task.task_id,
                ))} if observation.session_id else {}),
            },
            error=(
                {"code": observation.error_class or observation.state,
                 "message": observation.summary or observation.state}
                if status in {"blocked", "failed", "waiting"} else None
            ),
        )
        return observation

    def _sync_events(
        self, execution: Execution, attempt_id: str, task: TaskSpec, route: ModelRoute,
        session_id: str | None,
    ) -> int:
        if self.source_events is None:
            return execution.event_cursor
        source = self.source_events.replay(attempt_id, after=execution.event_cursor)
        for event in source:
            if (
                event.run_id != attempt_id or event.runtime_id != route.runtime_id
                or event.requirement_id != self.requirement_id or event.task_id != task.task_id
                or event.execution_id is not None
                or event.session_id != session_id
            ):
                raise WorkspaceError("Worker 原始事件身份与 attempt 不一致")
            self.events.append(AgentEvent(
                event.event_id, execution.id, event.runtime_id, event.kind,
                copy.deepcopy(event.payload), session_id=event.session_id,
                turn_id=event.turn_id, timestamp=event.timestamp,
                schema_version=event.schema_version, extra=copy.deepcopy(event.extra),
                requirement_id=self.requirement_id, task_id=task.task_id,
                execution_id=execution.id,
            ))
        return source[-1].sequence if source else execution.event_cursor


class IntegrationExecutionService:
    """为既有 Integration Gate 记录一等 Execution；不授予 merge 权限。"""

    def __init__(self, workspace: WorkspaceStore) -> None:
        self.workspace = workspace
        self.executions = ExecutionStore(workspace)

    def run(
        self, requirement_id: str, task_ids: tuple[str, ...], *, command_id: str,
        supervisor_snapshot: dict[str, Any], integrate: IntegrationPort,
    ) -> Execution:
        nodes = supervisor_snapshot.get("data", {}).get("nodes", {})
        # 旧 Requirement 可能没有 Supervisor nodes；此时不伪造 Task 结论，仍由既有
        # Integration Gate 自己拒绝或授权。已有 nodes 时必须全部 accepted。
        if task_ids and any(nodes.get(task_id, {}).get("status") != "accepted" for task_id in task_ids):
            raise WorkspaceError("Integration Execution 只能整合 Supervisor 已 accepted 的 Task")
        execution = self._execution(requirement_id, task_ids, command_id)
        # 已有 Gate 收据是终态事实；没有收据的 blocked/running/starting 可能来自
        # “Gate 已完成但包装层未落盘”的崩溃窗口，必须再次进入 Gate 自身的
        # request_id journal/reconcile，而不能由 Execution 状态永久阻断恢复。
        if "receipt" in execution.result:
            return execution
        if execution.status == "queued":
            execution, claimed = self.executions.claim_start(
                execution.id, role="integration", runtime_id="ai-dev-os-core",
                provider="ai-dev-os-core", model=None, reasoning_effort=None,
                workspace_path=self.workspace.working_root, prompt="整合已验收候选",
            )
            if not claimed:
                execution = self.executions.get(execution.id)
        execution = self.executions.update(
            execution.id, status="running", started_at=now_iso(), last_progress_at=now_iso(),
            completed_at=None, error=None,
        )
        try:
            receipt = integrate()
            return self.record(
                requirement_id, task_ids, command_id=command_id,
                supervisor_snapshot=supervisor_snapshot, receipt=receipt,
            )
        except Exception as exc:  # noqa: BLE001 -- 既有 Integration Provider 是外部副作用边界。
            return self.executions.update(
                execution.id, status="blocked", started_at=execution.started_at,
                completed_at=now_iso(), last_progress_at=now_iso(),
                summary=str(exc),
                error={"code": "integration_result_unknown", "message": str(exc)},
            )

    def record(
        self, requirement_id: str, task_ids: tuple[str, ...], *, command_id: str,
        supervisor_snapshot: dict[str, Any], receipt: dict[str, Any],
    ) -> Execution:
        """把既有 Gate 的 reconcile/recovery 收据回填到同一 Integration Execution。"""

        nodes = supervisor_snapshot.get("data", {}).get("nodes", {})
        if task_ids and any(nodes.get(task_id, {}).get("status") != "accepted" for task_id in task_ids):
            raise WorkspaceError("Integration Execution 只能整合 Supervisor 已 accepted 的 Task")
        execution = self._execution(requirement_id, task_ids, command_id)
        if not isinstance(receipt, dict):
            raise WorkspaceError("Integration Gate 必须返回结构化 receipt")
        validated = MergeReceipt.from_dict(receipt)
        if (
            validated.requirement_id != requirement_id.upper()
            or validated.request_id != command_id
        ):
            raise WorkspaceError("Integration receipt 身份与 Execution 不一致")
        status: ExecutionStatus = "completed" if validated.status == "merged" else "blocked"
        return self.executions.update(
            execution.id, status=status, started_at=execution.started_at or now_iso(),
            completed_at=now_iso(), last_progress_at=now_iso(),
            summary=(
                "Integration Gate 已完成" if status == "completed"
                else "Integration 已发生但 post-merge 恢复未完成"
            ),
            result={"receipt": copy.deepcopy(receipt)},
            error=(
                None if status == "completed" else
                {"code": "recovery_required", "message": validated.reason}
            ),
        )

    def _execution(
        self, requirement_id: str, task_ids: tuple[str, ...], command_id: str,
    ) -> Execution:
        execution = self.executions.create(
            requirement_id, "INTEGRATION", role="integration",
            runtime_id="ai-dev-os-core", prompt="整合已验收候选",
            source="integration", creation_key=f"integration:{requirement_id.upper()}:{command_id}",
            execution_policy={"task_ids": list(task_ids), "command_id": command_id},
        )
        if execution.execution_policy != {
            "task_ids": list(task_ids), "command_id": command_id,
        } or execution.requirement_id != requirement_id.upper():
            raise WorkspaceError("Integration command_id 已绑定不同请求")
        return execution
