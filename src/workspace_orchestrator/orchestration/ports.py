"""可替换策略和受信执行边界；策略与子进程不持有 Supervisor 的写权限。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..agent_runtime.contracts import RuntimeDescriptor
from .contracts import (
    ExecutionPlan,
    ModelRoute,
    PlanningRequest,
    PolicyDecision,
    RecoveryContext,
    RecoveryDecision,
    TaskSpec,
    VerificationPlan,
    VerificationPlanningRequest,
    VerificationReceiptEnvelope,
    WorkerIsolation,
    WorkerObservation,
)


class PlanningPolicy(Protocol):
    def plan(self, request: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]: ...


class ModelRouterProvider(Protocol):
    def route(
        self, task: TaskSpec, runtimes: tuple[RuntimeDescriptor, ...]
    ) -> tuple[ModelRoute, PolicyDecision]: ...


class VerificationPlannerProvider(Protocol):
    def plan(
        self, request: VerificationPlanningRequest
    ) -> tuple[VerificationPlan, PolicyDecision]: ...


class RecoveryPolicy(Protocol):
    def decide(self, context: RecoveryContext) -> tuple[RecoveryDecision, PolicyDecision]: ...


class VerificationExecutorPort(Protocol):
    """Phase 2 仅冻结证据协议；受控执行与来源证明由后续 Provider 完成。"""

    def execute(
        self, plan: VerificationPlan, *, workspace_path: Path
    ) -> VerificationReceiptEnvelope: ...


class WorkerExecutionPort(Protocol):
    """控制面中的受信 Adapter，不得把此对象或 Authority API 传给 Worker。

    同一 attempt_id 的重试 dispatch 必须幂等；更旧 fence 必须拒绝。
    poll/reconcile 不得重新执行任务；未知执行结果必须返回 unknown。
    isolation 来自受信 launcher 的真实检查，不接受 Worker JSON 自证隔离。
    cancel 只有真正终止后才能返回 failed，error_class 应标识 cancelled。
    """

    def isolation(self, task: TaskSpec) -> WorkerIsolation: ...

    def dispatch(
        self, attempt_id: str, fence: int, task: TaskSpec, route: ModelRoute
    ) -> WorkerObservation: ...

    def poll(self, attempt_id: str, fence: int) -> WorkerObservation: ...

    def cancel(self, attempt_id: str, fence: int) -> WorkerObservation: ...

    def reconcile(self, attempt_id: str, fence: int) -> WorkerObservation: ...
