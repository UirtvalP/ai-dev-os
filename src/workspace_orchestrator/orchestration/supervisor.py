"""本地确定性 Requirement Supervisor；Worker 只能提交候选，不能授予完成权限。"""

from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..agent_runtime.contracts import RuntimeDescriptor
from ..workspace import WorkspaceError
from .contracts import (
    ExecutionPlan,
    ModelRoute,
    PlanningRequest,
    PolicyDecision,
    PolicyError,
    RecoveryContext,
    RecoveryDecision,
    TaskSpec,
    VerificationCommand,
    VerificationPlan,
    VerificationPlanningRequest,
    VerificationReceiptEnvelope,
    WorkerIsolation,
    WorkerObservation,
    fingerprint,
)
from .policies import (
    BoundedRecoveryPolicy,
    CapabilityModelRouter,
    RulePlanningPolicy,
    RuleVerificationPlanner,
    validate_route,
)
from .ports import (
    ModelRouterProvider,
    PlanningPolicy,
    RecoveryPolicy,
    VerificationExecutorPort,
    VerificationPlannerProvider,
    WorkerExecutionPort,
)
from .store import OrchestrationStore, SupervisorLease

_NODE_STATES = frozenset({
    "pending", "dispatching", "running", "unknown", "candidate_complete", "verifying",
    "accepted", "blocked", "stopped", "replan_required", "retired",
})
_LIVE_STATES = frozenset({"dispatching", "running", "unknown"})
_TERMINAL_STATES = frozenset({"candidate_complete", "blocked", "failed"})
_UNSAFE_ERRORS = frozenset({"cancelled", "policy_violation", "invalid_contract", "isolation_failure"})


class SupervisorError(WorkspaceError):
    """控制面拒绝不完整、过期或越权的状态转换。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RequirementSupervisor:
    """显式 acquire 后才能变更；策略输出受 Core 预算、能力及授权边界二次检查。

    workers 与 candidate_reader/verification_executor 是受信控制面依赖，不得由
    Worker 数据动态指定。candidate_reader 必须独立检查真实 Git clean；脏工作树
    或无法确认时抛异常，绝不能返回 Worker 自报的 SHA/tree。
    """

    def __init__(
        self,
        store: OrchestrationStore,
        *,
        owner: str,
        workers: WorkerExecutionPort,
        runtimes: Callable[[], tuple[RuntimeDescriptor, ...]],
        planner: PlanningPolicy | None = None,
        router: ModelRouterProvider | None = None,
        recovery: RecoveryPolicy | None = None,
        max_workers: int = 1,
        protected_roots: tuple[Path, ...] = (),
        candidate_reader: Callable[[TaskSpec], tuple[str, str]] | None = None,
        verification_planner: VerificationPlannerProvider | None = None,
        verification_executor: VerificationExecutorPort | None = None,
        lease_ttl_seconds: float = 30,
        replan_budget: int = 1,
        clock: Callable[[], float] = time.time,
        allowed_worktree_roots: tuple[Path, ...] = (),
        max_plan_nodes: int = 64,
        execution_claim: Callable[[PlanningRequest, ExecutionPlan], None] | None = None,
    ) -> None:
        _integer(max_workers, "max_workers", 1)
        _integer(replan_budget, "replan_budget")
        _integer(max_plan_nodes, "max_plan_nodes", 1)
        self.store, self.owner, self.workers, self.runtimes = store, owner, workers, runtimes
        self.planner = planner or RulePlanningPolicy()
        self.router = router or CapabilityModelRouter()
        self.recovery = recovery or BoundedRecoveryPolicy()
        self.verification_planner = verification_planner or RuleVerificationPlanner()
        self.verification_executor, self.candidate_reader = verification_executor, candidate_reader
        self.max_workers, self.replan_budget = max_workers, replan_budget
        self.max_plan_nodes, self.lease_ttl_seconds = max_plan_nodes, lease_ttl_seconds
        self.clock = clock
        self.protected_roots = tuple(dict.fromkeys((
            *(_path(path) for path in protected_roots), store.root,
            Path(__file__).resolve().parent.parent,
        )))
        self.allowed_worktree_roots = tuple(_path(path) for path in allowed_worktree_roots)
        self.execution_claim = execution_claim
        self.lease: SupervisorLease | None = None
        self._verifications: set[str] = set()

    def acquire(self) -> SupervisorLease:
        self.lease = self.store.acquire(self.owner, self.lease_ttl_seconds)
        return self.lease

    def renew(self) -> SupervisorLease:
        self.lease = self.store.renew(self._lease(), self.lease_ttl_seconds)
        return self.lease

    def status(self) -> dict[str, Any]:
        snapshot = self.store.snapshot()
        _validate_data(snapshot["data"], snapshot["fence"])
        return snapshot

    def initialize(self, request: PlanningRequest) -> dict[str, Any]:
        request.validate()
        snapshot = self._active()
        data = snapshot["data"]
        if _initialized(data):
            if fingerprint(data["initial_request"]) != fingerprint(request.to_dict()):
                raise SupervisorError("already_initialized", "已有计划，变更必须使用有证据的 replan")
            self._claim_execution(request, ExecutionPlan.from_dict(data["plan"]))
            return snapshot
        plan, decision = self.planner.plan(copy.deepcopy(request))
        _plan_output(request, plan, decision)
        authorization = {
            # 保留既有摘要字段以兼容持久格式；单节点权限由 initial_request 来源独立授权。
            "write_allowed": any(task.write_required for task in request.tasks),
            "retry_budget": max(task.retry_budget for task in request.tasks),
            "replan_budget": self.replan_budget,
            "max_workers": self.max_workers,
            "max_plan_nodes": self.max_plan_nodes,
            "allowed_worktree_roots": [str(path) for path in self.allowed_worktree_roots],
            "exact_worktrees": [task.worktree for task in request.tasks if task.worktree],
            "protected_roots": [str(path) for path in self.protected_roots],
        }
        self._authorize_plan(plan, request, authorization, {})
        self._claim_execution(request, plan)
        now = self._now()

        def change(state: dict[str, Any]) -> None:
            if _initialized(state):
                raise SupervisorError("revision_conflict", "计划已被并发初始化")
            state.update(
                supervisor_schema_version=1, requirement_id=request.requirement_id,
                initial_request=request.to_dict(), plan=plan.to_dict(), authorization=authorization,
                replan_count=0, nodes={task.task_id: _node(task) for task in plan.nodes},
                plans=[{"request": request.to_dict(), "plan": plan.to_dict(),
                        "decision": decision.to_dict(), "at": now, "evidence": "initial"}],
                decisions=[{"kind": "planning", "at": now, "policy": decision.to_dict()}],
            )

        return self._change(change)

    def tick(self) -> dict[str, Any]:
        """一次有限步长：先收敛已知 attempt，再按依赖与槽位发送新的持久意图。"""

        snapshot = self._active(require_plan=True)
        self._claim_execution(PlanningRequest.from_dict(snapshot["data"]["initial_request"]),
                              ExecutionPlan.from_dict(snapshot["data"]["plan"]))
        for task_id, node in snapshot["data"]["nodes"].items():
            if node["active_attempt_id"] is not None:
                self._observe(task_id)
            elif node["status"] == "verifying":
                operation = node["verification"]["operation_id"]
                if operation not in self._verifications:
                    self._abandoned_verification(task_id)
        plan = ExecutionPlan.from_dict(self._active(require_plan=True)["data"]["plan"])
        for task in plan.nodes:
            current = self._active(require_plan=True)["data"]
            if current["nodes"][task.task_id]["status"] != "pending":
                continue
            limit = min(self.max_workers, current["authorization"]["max_workers"])
            if sum(node["active_attempt_id"] is not None for node in current["nodes"].values()) >= limit:
                break
            if any(current["nodes"][dependency]["status"] != "accepted" for dependency in task.depends_on):
                continue
            if self._workspace_busy(task, current):
                continue
            self._dispatch(task)
        return self.status()

    def replan(self, request: PlanningRequest, *, evidence: str) -> dict[str, Any]:
        """只接受显式新请求；保留历史、已验收节点、活跃 Worker 和未知验证。"""

        if not isinstance(evidence, str) or not evidence.strip():
            raise SupervisorError("evidence_required", "replan 必须记录人工选择或明确的新规划证据")
        request.validate()
        data = self._active(require_plan=True)["data"]
        if request.requirement_id != data["requirement_id"]:
            raise SupervisorError("wrong_requirement", "replan 不能迁移到另一 Requirement")
        budget = min(self.replan_budget, data["authorization"]["replan_budget"])
        if data["replan_count"] >= budget:
            raise SupervisorError("replan_budget", "重规划预算已耗尽")
        plan, decision = self.planner.plan(copy.deepcopy(request))
        _plan_output(request, plan, decision)
        self._authorize_plan(plan, PlanningRequest.from_dict(data["initial_request"]),
                             data["authorization"], data["nodes"],
                             previous_plans=tuple(ExecutionPlan.from_dict(item["plan"]) for item in data["plans"]))
        self._claim_execution(PlanningRequest.from_dict(data["initial_request"]), plan)
        before = fingerprint(data)
        now = self._now()

        def change(state: dict[str, Any]) -> None:
            if fingerprint(state) != before:
                raise SupervisorError("revision_conflict", "规划期间控制面状态已变化")
            wanted = {task.task_id for task in plan.nodes}
            for task_id, node in state["nodes"].items():
                if task_id not in wanted:
                    node["status"], node["revision"] = "retired", node["revision"] + 1
            for task in plan.nodes:
                old = state["nodes"].get(task.task_id)
                if old is None:
                    state["nodes"][task.task_id] = _node(task)
                elif not _frozen(old):
                    if old["status"] != "candidate_complete" or old["spec"] != task.to_dict():
                        old["status"] = "pending"
                    old["spec"], old["revision"] = task.to_dict(), old["revision"] + 1
                    old["reason"] = "显式 replan：" + evidence
            state["plan"] = plan.to_dict()
            state["replan_count"] += 1
            state["plans"].append({"request": request.to_dict(), "plan": plan.to_dict(),
                                   "decision": decision.to_dict(), "at": now, "evidence": evidence})
            state["decisions"].append({"kind": "replan", "at": now, "policy": decision.to_dict()})

        return self._change(change)

    def verify_task(
        self, task_id: str, commands: tuple[VerificationCommand, ...], environment: dict[str, str]
    ) -> dict[str, Any]:
        """受控验证前后独立读取候选；没有 Executor 或只有 Worker PASS 时拒绝验收。"""

        data = self._active(require_plan=True)["data"]
        node = _get_node(data, task_id)
        if node["status"] != "candidate_complete" or node["active_attempt_id"] is not None:
            raise SupervisorError("not_candidate", "只能验证已终止 Worker 的候选结果")
        if self.candidate_reader is None or self.verification_executor is None:
            raise SupervisorError("verification_unavailable", "未配置可信候选读取或隔离 Verification Executor")
        task = TaskSpec.from_dict(node["spec"])
        operation = "verify-" + uuid4().hex
        started = self._now()
        try:
            candidate = self.candidate_reader(task)
            request = VerificationPlanningRequest(
                data["requirement_id"], task_id, *candidate, dict(environment), commands,
            )
            if candidate != (node.get("candidate_sha"), node.get("candidate_tree")):
                raise SupervisorError("stale_candidate", "真实候选与 Worker 候选不一致")
            plan, decision = self.verification_planner.plan(copy.deepcopy(request))
            plan.validate()
            _audit(decision, request.to_dict(), plan.to_dict())
            for field in ("requirement_id", "task_id", "candidate_sha", "candidate_tree", "environment", "commands"):
                if getattr(plan, field) != getattr(request, field):
                    raise SupervisorError("verification_plan_changed", "验证策略不能静默替换显式命令或候选")
        except Exception as exc:  # noqa: BLE001 - 受信外部边界失败不能产生验收权限。
            return self._verification_failed(task_id, str(exc), None)

        def begin(state: dict[str, Any]) -> None:
            current = _get_node(state, task_id)
            if current["status"] != "candidate_complete" or current["revision"] != node["revision"]:
                raise SupervisorError("revision_conflict", "候选在验证准备期间已变化")
            current["status"], current["revision"] = "verifying", current["revision"] + 1
            if "verification" in current:
                current.setdefault("verification_history", []).append(copy.deepcopy(current["verification"]))
            current["verification"] = {"operation_id": operation, "status": "running",
                                       "plan": plan.to_dict(), "started_at": started,
                                       "fence": self._lease().fence}
            state["decisions"].append({"kind": "verification", "task_id": task_id,
                                       "at": started, "policy": decision.to_dict()})

        self._change(begin)
        self._verifications.add(operation)
        execution_returned = False
        try:
            self._active(require_plan=True)
            receipt = self.verification_executor.execute(copy.deepcopy(plan), workspace_path=_task_path(task))
            execution_returned = True
            receipt.validate_for(plan)
            completed = self._now()
            receipt_start = datetime.fromisoformat(receipt.started_at).timestamp()
            receipt_end = datetime.fromisoformat(receipt.completed_at).timestamp()
            if not started <= receipt_start <= receipt_end <= completed:
                raise SupervisorError("stale_verification", "回执时间不属于本次受控验证窗口")
            if self.candidate_reader(task) != candidate:
                raise SupervisorError("stale_candidate", "验证期间候选 SHA/tree 或 clean 状态变化")

            def accept(state: dict[str, Any]) -> None:
                current = _get_node(state, task_id)
                if (current["status"] != "verifying"
                        or current["verification"]["operation_id"] != operation):
                    raise SupervisorError("revision_conflict", "验证操作已被替换")
                current["verification"].update(
                    status="passed", completed_at=completed, receipt=receipt.to_dict()
                )
                current["status"], current["revision"] = "accepted", current["revision"] + 1
                current["reason"] = "可信验证完整通过；仍需 Integration Gate"

            return self._change(accept)
        except Exception as exc:  # noqa: BLE001 - 不能将执行异常或旧证据视为 PASS。
            if not execution_returned:
                self._abandoned_verification(task_id)
                return self.status()
            return self._verification_failed(task_id, str(exc), operation)
        finally:
            self._verifications.discard(operation)

    def close(self, *, cancel_running: bool = False) -> None:
        if self.lease is None:
            return
        data = self._active()["data"]
        if cancel_running and _initialized(data):
            for task_id, node in data["nodes"].items():
                if node["active_attempt_id"] is not None:
                    self._observe(task_id, cancel=True)
            if any(node["active_attempt_id"] is not None or _unknown_verification(node)
                   for node in self.status()["data"]["nodes"].values()):
                raise SupervisorError("termination_unknown", "仍有未确认终止的 Worker，保留租约与意图")
        self.store.release(self._lease())
        self.lease = None

    def _lease(self) -> SupervisorLease:
        if self.lease is None:
            raise SupervisorError("lease_required", "必须先 acquire Supervisor 租约")
        return self.lease

    def _now(self) -> float:
        stamp = self.clock()
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            raise SupervisorError("invalid_clock", "Supervisor clock 必须是有限非负时间")
        return float(stamp)

    def _active(self, *, require_plan: bool = False) -> dict[str, Any]:
        lease = self._lease()
        snapshot = self.status()
        current = snapshot["lease"]
        stamp = self._now()
        if stamp < snapshot["last_observed_at"]:
            raise SupervisorError("clock_rollback", "Supervisor 时钟早于最近提交，不能执行副作用")
        if (current is None or (current["owner"], current["fence"], current["expires_at"])
                != (lease.owner, lease.fence, lease.expires_at) or stamp >= lease.expires_at):
            raise SupervisorError("lease_lost", "Supervisor 租约已失效，不能执行副作用")
        if require_plan and not _initialized(snapshot["data"]):
            raise SupervisorError("not_initialized", "必须先 initialize 执行计划")
        return snapshot

    def _change(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        lease = self._lease()

        def checked(data: dict[str, Any]) -> None:
            _validate_data(data, lease.fence)
            change(data)
            if _initialized(data):
                data["status"] = _summary(data)
            _validate_data(data, lease.fence)

        return self.store.mutate(lease, checked)

    def _claim_execution(self, initial: PlanningRequest, plan: ExecutionPlan) -> None:
        """受信装配在发布可执行状态前建立 V2 归属；策略不能通过 extra 自证所有权。"""

        if self.execution_claim is not None:
            self._active()
            self.execution_claim(copy.deepcopy(initial), copy.deepcopy(plan))

    def _authorize_plan(
        self, plan: ExecutionPlan, initial: PlanningRequest,
        authorization: dict[str, Any], previous: dict[str, Any],
        *, previous_plans: tuple[ExecutionPlan, ...] = (),
    ) -> None:
        if plan.requirement_id != initial.requirement_id:
            raise SupervisorError("wrong_requirement", "Planner 不能改写 Requirement 身份")
        limit = min(self.max_plan_nodes, authorization["max_plan_nodes"])
        if len(set(previous) | {task.task_id for task in plan.nodes}) > limit:
            raise SupervisorError("plan_budget", "计划及历史 Task 总数超出授权上限")
        nodes = {task.task_id: task for task in plan.nodes}
        for task_id, node in previous.items():
            if _frozen(node) and (task_id not in nodes or nodes[task_id].to_dict() != node["spec"]):
                raise SupervisorError("frozen_node", "不能移除或改写 accepted/活跃/未知验证节点")
        bindings: dict[str, tuple[str, ...]] = {}
        locations: list[tuple[Path, tuple[str, ...]]] = []
        for historical_plan in previous_plans:
            for historical_task in historical_plan.nodes:
                _bind_source(historical_task, initial, authorization, bindings, locations)
        for node in previous.values():
            _bind_source(TaskSpec.from_dict(node["spec"]), initial, authorization, bindings, locations)
        for task in plan.nodes:
            _bind_source(task, initial, authorization, bindings, locations)
            self._authorize_task(task, authorization, initial)

    def _authorize_task(
        self, task: TaskSpec, authorization: dict[str, Any], initial: PlanningRequest
    ) -> Path:
        path = _task_path(task)
        _source_scope(task, initial, authorization, _authorize_source(task, initial))
        protected = (*self.protected_roots,
                     *(_path(Path(item)) for item in authorization["protected_roots"]))
        if any(_overlaps(path, item) for item in protected):
            raise SupervisorError("protected_workspace", "Task worktree 与受保护控制面路径重叠")
        if task.write_required and (not task.branch or task.branch in ("main", "refs/heads/main")):
            raise SupervisorError("unisolated_branch", "写任务必须明确使用独立非 main branch")
        return path

    def _workspace_busy(self, task: TaskSpec, data: dict[str, Any]) -> bool:
        path = _task_path(task)
        for task_id, node in data["nodes"].items():
            if (task_id != task.task_id and (node["active_attempt_id"] is not None
                    or node["status"] in ("candidate_complete", "verifying") or _unknown_verification(node))
                    and _overlaps(path, _task_path(TaskSpec.from_dict(node["spec"])))):
                return True
        return False

    def _isolation(self, task: TaskSpec, data: dict[str, Any]) -> WorkerIsolation:
        path = self._authorize_task(task, data["authorization"], PlanningRequest.from_dict(data["initial_request"]))
        isolation = self.workers.isolation(copy.deepcopy(task))
        isolation.validate()
        if not isolation.enforced:
            raise SupervisorError("isolation_failure", "可信 Worker launcher 未证明强制隔离")
        protected = (*self.protected_roots,
                     *(_path(Path(item)) for item in data["authorization"]["protected_roots"]),
                     *(_path(Path(item)) for item in isolation.protected_roots))
        for item in isolation.writable_roots:
            writable = _path(Path(item))
            if not (writable == path or path in writable.parents):
                raise SupervisorError("isolation_failure", "Worker 可写路径逃逸独立 Task worktree")
            if any(_overlaps(writable, item) for item in protected):
                raise SupervisorError("isolation_failure", "Worker 可写路径覆盖控制面或 trust policy")
        if task.write_required and not any(_path(Path(item)) == path for item in isolation.writable_roots):
            raise SupervisorError("isolation_failure", "写任务未获独立 worktree 写权限")
        return isolation

    def _dispatch(self, task: TaskSpec) -> None:
        data = self._active(require_plan=True)["data"]
        self._claim_execution(PlanningRequest.from_dict(data["initial_request"]),
                              ExecutionPlan.from_dict(data["plan"]))
        node = data["nodes"][task.task_id]
        if len(node["attempts"]) >= 1 + task.retry_budget + data["replan_count"]:
            self._block(task.task_id, "执行与重规划预算已耗尽", stopped=True)
            return
        try:
            runtimes = self.runtimes()
            route, decision = self.router.route(copy.deepcopy(task), copy.deepcopy(runtimes))
            validate_route(task, route, runtimes)
            _audit(decision, {"task": task.to_dict(), "runtimes": [asdict(item) for item in runtimes]}, route.to_dict())
            isolation = self._isolation(task, data)
        except Exception as exc:  # noqa: BLE001 - 外部策略或隔离不可用时不能启动 Worker。
            self._block(task.task_id, f"启动前检查拒绝：{exc}")
            return
        attempt_id, stamp = "attempt-" + uuid4().hex, self._now()
        fence = self._lease().fence

        def intent(state: dict[str, Any]) -> None:
            current = _get_node(state, task.task_id)
            if current["status"] != "pending" or self._workspace_busy(task, state):
                raise SupervisorError("dispatch_conflict", "Task 已不再可发送")
            if any(state["nodes"][dependency]["status"] != "accepted" for dependency in task.depends_on):
                raise SupervisorError("dependency_unaccepted", "依赖尚未受控验收")
            limit = min(self.max_workers, state["authorization"]["max_workers"])
            if sum(item["active_attempt_id"] is not None for item in state["nodes"].values()) >= limit:
                raise SupervisorError("concurrency_limit", "Worker 并发槽位已满")
            current["attempts"].append({"attempt_id": attempt_id, "fence": fence,
                                        "route": route.to_dict(), "created_at": stamp,
                                        "updated_at": stamp, "state": "dispatching",
                                        "task_spec": task.to_dict(), "plan_id": state["plan"]["plan_id"],
                                        "plan_generation": state["replan_count"],
                                        "isolation": isolation.to_dict()})
            current["active_attempt_id"], current["status"] = attempt_id, "dispatching"
            current["revision"] += 1
            state["decisions"].append({"kind": "routing", "task_id": task.task_id,
                                       "at": stamp, "policy": decision.to_dict()})

        self._change(intent)
        self._active(require_plan=True)
        try:
            observation = self.workers.dispatch(attempt_id, fence, copy.deepcopy(task), copy.deepcopy(route))
        except Exception as exc:  # noqa: BLE001 - 发送异常无法证明 Worker 未启动。
            observation = WorkerObservation(attempt_id, fence, "unknown", error_class="ambiguous_result", summary=str(exc))
        self._apply(task.task_id, observation)

    def _observe(self, task_id: str, *, cancel: bool = False) -> None:
        data = self._active(require_plan=True)["data"]
        node = _get_node(data, task_id)
        attempt = _attempt(node)
        if attempt is None:
            return
        stale = attempt["fence"] != self._lease().fence
        try:
            self._active(require_plan=True)
            if stale or cancel:
                observation = self.workers.cancel(attempt["attempt_id"], attempt["fence"])
            elif attempt["state"] in ("dispatching", "unknown"):
                observation = self.workers.reconcile(attempt["attempt_id"], attempt["fence"])
            else:
                observation = self.workers.poll(attempt["attempt_id"], attempt["fence"])
            observation.validate()
            if (observation.attempt_id, observation.fence) != (attempt["attempt_id"], attempt["fence"]):
                raise SupervisorError("stale_worker", "Worker observation 的 attempt/fence 不匹配")
        except Exception as exc:  # noqa: BLE001 - 失联不意味着已终止，保留槽位与目录。
            self._unknown(task_id, str(exc))
            return
        if stale or cancel:
            if observation.state == "failed" and observation.error_class == "cancelled":
                self._retire(task_id, observation, retry=stale and not cancel)
            else:
                self._unknown(task_id, "取消未确认子树终止；不得重发或接纳旧 epoch 结果")
        else:
            self._apply(task_id, observation)

    def _apply(self, task_id: str, observation: WorkerObservation) -> None:
        try:
            observation.validate()
            current = _get_node(self._active(require_plan=True)["data"], task_id)
            attempt = _attempt(current)
            if (attempt is None or observation.attempt_id != attempt["attempt_id"]
                    or observation.fence != self._lease().fence or observation.fence != attempt["fence"]):
                raise SupervisorError("stale_worker", "拒绝旧 fence 或非当前 attempt 的 Worker 结果")
        except (PolicyError, SupervisorError) as exc:
            if isinstance(exc, SupervisorError) and exc.code == "lease_lost":
                raise
            self._unknown(task_id, str(exc))
            return
        stamp = self._now()

        def apply(state: dict[str, Any]) -> None:
            node = _get_node(state, task_id)
            active = _attempt(node)
            if active is None or active["attempt_id"] != observation.attempt_id:
                raise SupervisorError("dispatch_conflict", "Worker attempt 已变化")
            active.update(state=observation.state, observation=observation.to_dict(), updated_at=stamp)
            node["revision"] += 1
            if observation.state in _TERMINAL_STATES:
                node["active_attempt_id"] = None
            if observation.state == "candidate_complete":
                node.update(status="candidate_complete", candidate_sha=observation.candidate_sha,
                            candidate_tree=observation.candidate_tree, reason=observation.summary)
            elif observation.state in ("running", "unknown"):
                if observation.state == "unknown":
                    self._recover(state, node, "unknown")
                node["status"] = observation.state
            else:
                self._recover(state, node, observation.error_class or "manual_block",
                              forced_block=observation.state == "blocked")

        self._change(apply)

    def _retire(self, task_id: str, observation: WorkerObservation, *, retry: bool) -> None:
        stamp = self._now()

        def change(state: dict[str, Any]) -> None:
            node = _get_node(state, task_id)
            attempt = _attempt(node)
            if attempt is None or attempt["attempt_id"] != observation.attempt_id:
                raise SupervisorError("dispatch_conflict", "待取消的 attempt 已变化")
            attempt.update(state="failed", cancel_observation=observation.to_dict(),
                           retired_by_fence=self._lease().fence, updated_at=stamp)
            node["active_attempt_id"], node["revision"] = None, node["revision"] + 1
            self._recover(state, node, "crash" if retry else "cancelled")

        self._change(change)

    def _unknown(self, task_id: str, reason: str) -> None:
        def change(state: dict[str, Any]) -> None:
            node = _get_node(state, task_id)
            attempt = _attempt(node)
            if attempt is None:
                raise SupervisorError("dispatch_conflict", "未知观察不对应活跃 attempt")
            self._recover(state, node, "unknown")
            node.update(status="unknown", reason=reason, revision=node["revision"] + 1)
            attempt.update(state="unknown", updated_at=self._now(), uncertainty=reason)

        self._change(change)

    def _block(self, task_id: str, reason: str, *, stopped: bool = False) -> None:
        def change(state: dict[str, Any]) -> None:
            node = _get_node(state, task_id)
            node.update(status="stopped" if stopped else "blocked", reason=reason,
                        revision=node["revision"] + 1)

        self._change(change)

    def _recover(self, state: dict[str, Any], node: dict[str, Any], error: str, *, forced_block: bool = False) -> None:
        task = TaskSpec.from_dict(node["spec"])
        context = RecoveryContext(error, node["retry_count"], task.retry_budget,
                                  duplicate_risk=node["active_attempt_id"] is not None,
                                  replan_count=state["replan_count"],
                                  replan_budget=min(self.replan_budget, state["authorization"]["replan_budget"]))
        try:
            choice, decision = self.recovery.decide(copy.deepcopy(context))
            choice.validate()
            _audit(decision, context.to_dict(), choice.to_dict())
        except Exception as exc:  # noqa: BLE001 - 策略故障只能升级人工阻塞。
            choice = RecoveryDecision("escalate", f"恢复策略无效：{exc}")
            decision = PolicyDecision("core.recovery-fail-closed", "1", choice.reason,
                                      fingerprint(context.to_dict()), choice.to_dict())
        action = choice.action
        if context.duplicate_risk or forced_block:
            action = "escalate"
        elif error in _UNSAFE_ERRORS or (action == "retry" and context.attempts >= context.retry_budget):
            action = "stop"
        elif action == "replan" and context.replan_count >= context.replan_budget:
            action = "escalate"
        if action == "retry":
            node["retry_count"] += 1
        node["status"] = {"retry": "pending", "replan": "replan_required",
                          "escalate": "blocked", "stop": "stopped"}[action]
        node["reason"] = choice.reason
        record = {"kind": "recovery", "task_id": task.task_id, "policy": decision.to_dict(),
                  "effective_action": action, "error_class": error}
        last = state["decisions"][-1] if state["decisions"] else {}
        if any(last.get(key) != value for key, value in record.items()):
            state["decisions"].append({**record, "at": self._now()})

    def _verification_failed(self, task_id: str, reason: str, operation: str | None) -> dict[str, Any]:
        def change(state: dict[str, Any]) -> None:
            node = _get_node(state, task_id)
            if operation is not None:
                if node.get("verification", {}).get("operation_id") != operation:
                    raise SupervisorError("revision_conflict", "验证操作已变化")
                node["verification"].update(status="failed", reason=reason, completed_at=self._now())
            node["revision"] += 1
            self._recover(state, node, "verification_failed")
            node["reason"] = reason

        return self._change(change)

    def _abandoned_verification(self, task_id: str) -> None:
        def change(state: dict[str, Any]) -> None:
            node = _get_node(state, task_id)
            node["verification"].update(status="unknown", reason="控制面重启后验证结果未知")
            node.update(status="blocked", revision=node["revision"] + 1,
                        reason="验证执行结果未知；当前 Executor 无 reconcile，保留目录独占并失败关闭")

        self._change(change)


def _node(task: TaskSpec) -> dict[str, Any]:
    return {"spec": task.to_dict(), "status": "pending", "revision": 1,
            "attempts": [], "active_attempt_id": None, "retry_count": 0, "reason": "",
            "verification_history": []}


def _attempt(node: dict[str, Any]) -> dict[str, Any] | None:
    identifier = node["active_attempt_id"]
    return next((item for item in node["attempts"] if item["attempt_id"] == identifier), None)


def _get_node(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    if task_id not in data["nodes"]:
        raise SupervisorError("unknown_task", f"没有此执行节点：{task_id}")
    node: dict[str, Any] = data["nodes"][task_id]
    return node


def _path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or path.is_symlink() or path.resolve() != path:
        raise SupervisorError("unsafe_path", "控制面与 Task 路径必须是无重定向的绝对路径")
    return path


def _task_path(task: TaskSpec) -> Path:
    if task.worktree is None:
        raise SupervisorError("workspace_required", "执行任务必须显式指定独立 worktree")
    path = _path(Path(task.worktree))
    if not path.is_dir():
        raise SupervisorError("workspace_missing", "Task worktree 目录尚不存在")
    return path


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _audit(decision: PolicyDecision, inputs: dict[str, Any], output: dict[str, Any]) -> None:
    decision.validate()
    if decision.input_fingerprint != fingerprint(inputs) or fingerprint(decision.decision) != fingerprint(output):
        raise SupervisorError("invalid_policy_decision", "策略审计记录没有绑定本次真实输入与输出")


def _plan_output(request: PlanningRequest, plan: ExecutionPlan, decision: PolicyDecision) -> None:
    plan.validate()
    if plan.requirement_id != request.requirement_id:
        raise SupervisorError("wrong_requirement", "Planner 输出的 Requirement 身份错误")
    _audit(decision, request.to_dict(), plan.to_dict())


def _integer(value: Any, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise SupervisorError("invalid_state", f"{name} 必须是大于等于 {minimum} 的整数")


def _initialized(data: dict[str, Any]) -> bool:
    return "supervisor_schema_version" in data


def _unknown_verification(node: dict[str, Any]) -> bool:
    return node.get("verification", {}).get("status") in ("running", "unknown")


def _frozen(node: dict[str, Any]) -> bool:
    return node["status"] == "accepted" or node["active_attempt_id"] is not None or _unknown_verification(node)


def _source_ids(task: TaskSpec, initial: PlanningRequest) -> tuple[str, ...]:
    """身份已存在时只能引用自身；多来源请求不能靠重命名猜测拆解授权。"""

    originals = {item.task_id: item for item in initial.tasks}
    sources: tuple[str, ...]
    for original in initial.tasks:
        if original.source_task_ids and original.source_task_ids != (original.task_id,):
            raise SupervisorError("invalid_source", "初始授权 Task 不能借用其他 Task 的来源")
    if task.task_id in originals:
        sources = (task.task_id,)
        if task.source_task_ids and task.source_task_ids != sources:
            raise SupervisorError("source_changed", "已有 Task 不能更换授权来源")
    elif task.source_task_ids:
        sources = tuple(sorted(task.source_task_ids))
    elif len(originals) == 1:
        sources = tuple(originals)
    else:
        raise SupervisorError("source_required", "多个初始 Task 的派生节点必须明确 source_task_ids")
    if any(source not in originals for source in sources):
        raise SupervisorError("invalid_source", "source_task_ids 只能指向初始请求中的授权 Task")
    return sources


def _authorize_source(task: TaskSpec, initial: PlanningRequest) -> tuple[str, ...]:
    sources = _source_ids(task, initial)
    originals = {item.task_id: item for item in initial.tasks}
    grants = tuple(originals[source] for source in sources)
    if task.write_required and not all(source.write_required for source in grants):
        raise SupervisorError("permission_expansion", "Planner 不能扩大此 Task 或任一来源的写权限")
    if task.retry_budget > min(source.retry_budget for source in grants):
        raise SupervisorError("retry_budget", "Planner 不能借用其他 Task 的 retry budget")
    for name in ("preferred_runtime", "preferred_model", "preferred_effort"):
        if task.task_id in originals:
            # None 是此已有节点的默认路由选择，不从其他节点继承约束或偏好。
            if getattr(task, name) != getattr(originals[task.task_id], name):
                raise SupervisorError("preference_changed", "已有 Task 必须保留自己的显式或默认偏好")
        else:
            explicit = {getattr(source, name) for source in grants if getattr(source, name) is not None}
            if len(explicit) > 1:
                raise SupervisorError("source_preference_conflict", "来源存在互不兼容的显式偏好，不能合并为单个 Task")
            if explicit and getattr(task, name) != next(iter(explicit)):
                raise SupervisorError("preference_changed", "派生 Task 必须遵守其来源的显式偏好")
    return sources


def _scope_path(value: str) -> Path:
    """来源授权的纯词法范围校验；启动前另行检查物理目录和重定向。"""

    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SupervisorError("unsafe_path", "来源授权目录必须是无父级跳转的绝对路径")
    return path


def _source_scope(
    task: TaskSpec, initial: PlanningRequest, authorization: dict[str, Any], sources: tuple[str, ...],
) -> Path:
    if task.worktree is None:
        raise SupervisorError("workspace_required", "执行计划必须为 Task 明确分配 worktree")
    path = _scope_path(task.worktree)
    own = tuple(_scope_path(item.worktree) for item in initial.tasks
                if item.task_id in sources and item.worktree is not None)
    roots = tuple(_scope_path(item) for item in authorization["allowed_worktree_roots"])
    if not (any(path == root or root in path.parents for root in roots) if roots else path in own):
        raise SupervisorError("unauthorized_workspace", "Task worktree 不在其来源对应的授权目录")
    for other in initial.tasks:
        if other.task_id not in sources and other.worktree is not None:
            other_path = _scope_path(other.worktree)
            # 操作员原本就显式共用的目录仍由互斥调度管理；Planner 不能制造新的串权。
            if _overlaps(path, other_path):
                shared = path if other_path == path or other_path in path.parents else other_path
                if not any(shared == origin or origin in shared.parents for origin in own):
                    raise SupervisorError("source_workspace_conflict", "派生 Task 不能占用其他来源的原始 worktree")
    if any(_overlaps(path, _scope_path(item)) for item in authorization["protected_roots"]):
        raise SupervisorError("protected_workspace", "来源授权不能覆盖受保护控制面")
    return path


def _bind_source(
    task: TaskSpec, initial: PlanningRequest, authorization: dict[str, Any],
    bindings: dict[str, tuple[str, ...]], locations: list[tuple[Path, tuple[str, ...]]],
) -> None:
    """按整个规划历史固定 Task 来源和派生目录归属，不因 retire/replan 清空。"""

    sources = _authorize_source(task, initial)
    if task.task_id in bindings and bindings[task.task_id] != sources:
        raise SupervisorError("source_changed", "replan 不能改写已有派生 Task 的授权来源")
    path = _source_scope(task, initial, authorization, sources)

    def initially_shared(location: Path, owners: tuple[str, ...]) -> bool:
        return any(item.task_id in owners and item.worktree is not None
                   and (location == _scope_path(item.worktree) or _scope_path(item.worktree) in location.parents)
                   for item in initial.tasks)

    for previous_path, previous_sources in locations:
        shared = path if previous_path == path or previous_path in path.parents else previous_path
        if (previous_sources != sources and _overlaps(previous_path, path)
                and not all(initially_shared(shared, owners) for owners in (sources, previous_sources))):
            raise SupervisorError("source_workspace_conflict", "规划不能转移其他来源已经使用的派生目录")
    bindings[task.task_id] = sources
    if (path, sources) not in locations:
        locations.append((path, sources))


def _summary(data: dict[str, Any]) -> str:
    nodes = [data["nodes"][item["task_id"]] for item in data["plan"]["nodes"]]
    if all(node["status"] == "accepted" for node in nodes):
        return "ready_for_integration"
    if any(node["active_attempt_id"] is not None or node["status"] == "verifying" for node in nodes):
        return "running"
    if any(node["status"] in ("blocked", "stopped", "replan_required") for node in nodes):
        return "blocked"
    return "in_progress"


def _validate_data(data: dict[str, Any], fence: int) -> None:
    """不猜测修复损坏的调度事实；未知扩展字段由原对象保留。"""

    if not _initialized(data):
        if any(name in data for name in ("nodes", "plan", "initial_request", "authorization")):
            raise SupervisorError("invalid_state", "控制面状态缺少 schema 标识")
        return
    try:
        if type(data["supervisor_schema_version"]) is not int or data["supervisor_schema_version"] != 1:
            raise SupervisorError("unsupported_schema", "不支持的 Supervisor 数据版本")
        plan = ExecutionPlan.from_dict(data["plan"])
        initial = PlanningRequest.from_dict(data["initial_request"])
        if plan.requirement_id != initial.requirement_id or data["requirement_id"] != initial.requirement_id:
            raise SupervisorError("invalid_state", "持久计划与 Requirement 身份不一致")
        authorization = data["authorization"]
        for name in ("retry_budget", "replan_budget", "max_workers", "max_plan_nodes"):
            _integer(authorization[name], name, 1 if name in ("max_workers", "max_plan_nodes") else 0)
        if type(authorization["write_allowed"]) is not bool:
            raise SupervisorError("invalid_state", "write_allowed 必须是布尔值")
        if (authorization["write_allowed"] != any(task.write_required for task in initial.tasks)
                or authorization["retry_budget"] != max(task.retry_budget for task in initial.tasks)):
            raise SupervisorError("invalid_state", "持久权限或 retry 授权与原始请求不一致")
        for name in ("allowed_worktree_roots", "exact_worktrees", "protected_roots"):
            if not isinstance(authorization[name], list) or any(not isinstance(item, str) for item in authorization[name]):
                raise SupervisorError("invalid_state", "持久授权路径必须是字符串列表")
        if authorization["exact_worktrees"] != [task.worktree for task in initial.tasks if task.worktree]:
            raise SupervisorError("invalid_state", "原始 worktree 摘要与初始来源不一致")
        _integer(data["replan_count"], "replan_count")
        if data["replan_count"] > authorization["replan_budget"]:
            raise SupervisorError("invalid_state", "持久 replan_count 超出授权预算")
        if not isinstance(data["nodes"], dict) or not isinstance(data["plans"], list) or not data["plans"]:
            raise SupervisorError("invalid_state", "持久节点或计划历史无效")
        if len(data["nodes"]) > authorization["max_plan_nodes"] or len(data["plans"]) != data["replan_count"] + 1:
            raise SupervisorError("invalid_state", "持久节点/计划数量与预算不一致")
        if not isinstance(data["decisions"], list):
            raise SupervisorError("invalid_state", "策略决策历史无效")
        if data["plans"][-1]["plan"] != data["plan"]:
            raise SupervisorError("invalid_state", "当前计划不对应最后一次规划历史")
        source_bindings: dict[str, tuple[str, ...]] = {}
        source_locations: list[tuple[Path, tuple[str, ...]]] = []
        for history in data["plans"]:
            historical_request = PlanningRequest.from_dict(history["request"])
            historical_plan = ExecutionPlan.from_dict(history["plan"])
            _plan_output(historical_request, historical_plan, PolicyDecision.from_dict(history["decision"]))
            if historical_plan.requirement_id != data["requirement_id"]:
                raise SupervisorError("invalid_state", "规划历史跨越不同 Requirement")
            for historical_task in historical_plan.nodes:
                _bind_source(historical_task, initial, authorization, source_bindings, source_locations)
        for decision in data["decisions"]:
            PolicyDecision.from_dict(decision["policy"])
        identifiers: set[str] = set()
        for task_id, node in data["nodes"].items():
            task = TaskSpec.from_dict(node["spec"])
            if task.task_id != task_id or node["status"] not in _NODE_STATES:
                raise SupervisorError("invalid_state", "节点身份或状态无效")
            if task_id not in source_bindings:
                raise SupervisorError("invalid_state", "节点缺少受控规划历史与授权来源")
            _bind_source(task, initial, authorization, source_bindings, source_locations)
            _integer(node["revision"], "node revision", 1)
            _integer(node["retry_count"], "retry_count")
            sources = source_bindings[task_id]
            retry_limit = min(item.retry_budget for item in initial.tasks if item.task_id in sources)
            if node["retry_count"] > retry_limit or not isinstance(node["attempts"], list):
                raise SupervisorError("invalid_state", "节点 retry 计数或 attempt 列表无效")
            for attempt in node["attempts"]:
                identifier = attempt["attempt_id"]
                if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                    raise SupervisorError("invalid_state", "attempt 身份缺失或重复")
                identifiers.add(identifier)
                _integer(attempt["fence"], "attempt fence", 1)
                if attempt["fence"] > fence or attempt["state"] not in _LIVE_STATES | _TERMINAL_STATES:
                    raise SupervisorError("invalid_state", "attempt epoch 或状态不合法")
                if (attempt["state"] in _LIVE_STATES) != (identifier == node["active_attempt_id"]):
                    raise SupervisorError("invalid_state", "活跃 attempt 必须独占节点指针")
                attempt_spec = TaskSpec.from_dict(attempt["task_spec"])
                _integer(attempt["plan_generation"], "attempt plan_generation")
                generation = attempt["plan_generation"]
                if (attempt_spec.task_id != task_id or generation > data["replan_count"]
                        or attempt["plan_id"] != data["plans"][generation]["plan"]["plan_id"]
                        or attempt_spec.to_dict() not in data["plans"][generation]["plan"]["nodes"]):
                    raise SupervisorError("invalid_state", "attempt 未绑定其实际规划版本与 TaskSpec")
                ModelRoute.from_dict(attempt["route"])
                WorkerIsolation.from_dict(attempt["isolation"])
                for name in ("created_at", "updated_at"):
                    value = attempt[name]
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise SupervisorError("invalid_state", "attempt 时间无效")
                if "observation" in attempt:
                    observation = WorkerObservation.from_dict(attempt["observation"])
                    if observation.attempt_id != identifier or observation.fence != attempt["fence"]:
                        raise SupervisorError("invalid_state", "持久观察与 attempt 身份不一致")
            active = _attempt(node)
            if ((node["active_attempt_id"] is not None and active is None)
                    or (active is not None and active["state"] not in _LIVE_STATES)
                    or (active is not None) != (node["status"] in _LIVE_STATES)):
                raise SupervisorError("invalid_state", "节点活跃指针与状态不一致")
            if node["status"] == "accepted":
                verification = node["verification"]
                if verification["status"] != "passed":
                    raise SupervisorError("invalid_state", "accepted 缺少受控 PASS")
                verification_plan = VerificationPlan.from_dict(verification["plan"])
                VerificationReceiptEnvelope.from_dict(verification["receipt"]).validate_for(verification_plan)
                if (verification_plan.task_id != task_id or verification_plan.requirement_id != data["requirement_id"]
                        or (verification_plan.candidate_sha, verification_plan.candidate_tree)
                        != (node["candidate_sha"], node["candidate_tree"])):
                    raise SupervisorError("invalid_state", "accepted 回执没有绑定此节点的候选")
            if node["status"] == "verifying" and node["verification"]["status"] != "running":
                raise SupervisorError("invalid_state", "verifying 缺少执行意图")
            if _unknown_verification(node) and node["status"] != (
                "verifying" if node["verification"]["status"] == "running" else "blocked"
            ):
                raise SupervisorError("invalid_state", "未确认验证不能变成可重发或可验收节点")
            if node["status"] in ("candidate_complete", "verifying", "accepted"):
                latest = node["attempts"][-1]
                candidate = WorkerObservation.from_dict(latest["observation"])
                if (candidate.state != "candidate_complete" or (candidate.candidate_sha, candidate.candidate_tree)
                        != (node["candidate_sha"], node["candidate_tree"])):
                    raise SupervisorError("invalid_state", "候选节点没有绑定最近实际终止的 Worker")
        for task in plan.nodes:
            node = data["nodes"][task.task_id]
            if node["spec"] != task.to_dict() or node["status"] == "retired":
                raise SupervisorError("invalid_state", "当前计划节点与持久 spec 不一致")
        if data["status"] != _summary(data):
            raise SupervisorError("invalid_state", "持久 Requirement 投影与节点状态不一致")
    except (KeyError, IndexError, TypeError, AttributeError, OverflowError, PolicyError) as exc:
        raise SupervisorError("invalid_state", f"Supervisor 持久状态损坏：{exc}") from exc
