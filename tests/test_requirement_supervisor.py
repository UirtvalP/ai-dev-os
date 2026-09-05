"""Supervisor 公开接口的确定性 E2E；fake 不提供实际 OS 隔离证明。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from workspace_orchestrator.agent_runtime.contracts import ModelDescriptor, RuntimeDescriptor
from workspace_orchestrator.orchestration.contracts import (
    ExecutionPlan,
    ModelRoute,
    PlanningRequest,
    PolicyDecision,
    RecoveryContext,
    RecoveryDecision,
    TaskSpec,
    VerificationCommand,
    VerificationCommandResult,
    VerificationPlan,
    VerificationReceiptEnvelope,
    WorkerIsolation,
    WorkerObservation,
    fingerprint,
)
from workspace_orchestrator.orchestration.policies import RulePlanningPolicy
from workspace_orchestrator.orchestration.store import OrchestrationStore
from workspace_orchestrator.orchestration.supervisor import RequirementSupervisor
from workspace_orchestrator.workspace import WorkspaceError

SHA = "a" * 40
TREE = "b" * 40
COMMANDS = (VerificationCommand("unit", ("fixture-python", "-m", "pytest"), 30),)
ENVIRONMENT = {"os": "fixture", "python": "fixture-3.11"}


@dataclass
class Clock:
    now: float = 1_788_609_600.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def runtime(**changes: Any) -> RuntimeDescriptor:
    return replace(RuntimeDescriptor(
        "fixture-runtime", "Fixture Runtime", "1", True,
        ("start", "message", "events", "profile:read-only", "profile:workspace-write"),
        (ModelDescriptor("reported-model", "Reported Model", ("low", "high"), True),),
    ), **changes)


class FakeWorkers:
    """受信 launcher 假件；只有显式观测变更才声明进程结束。"""

    def __init__(self, store: OrchestrationStore, protected: tuple[Path, ...]) -> None:
        self.store = store
        self.protected = protected
        self.dispatches: list[tuple[str, int, TaskSpec, ModelRoute]] = []
        self.polls: list[tuple[str, int]] = []
        self.reconciliations: list[tuple[str, int]] = []
        self.cancellations: list[tuple[str, int]] = []
        self.observations: dict[str, WorkerObservation] = {}
        self.isolation_enforced = True
        self.cancel_confirmed = True
        self.fail_after_dispatch = False
        self.extra_writable_root: Path | None = None

    def isolation(self, task: TaskSpec) -> WorkerIsolation:
        writable = (str(task.worktree),) if task.write_required else ()
        if self.extra_writable_root is not None:
            writable += (str(self.extra_writable_root),)
        return WorkerIsolation(
            "fixture-trusted-launcher", self.isolation_enforced, writable,
            tuple(str(path) for path in self.protected),
        )

    def dispatch(
        self, attempt_id: str, fence: int, task: TaskSpec, route: ModelRoute,
    ) -> WorkerObservation:
        # 在有外部副作用的调用前，Supervisor 必须已经持久化 dispatch intent。
        node = self.store.snapshot()["data"]["nodes"][task.task_id]
        assert node["status"] == "dispatching"
        assert node["active_attempt_id"] == attempt_id
        assert node["attempts"]
        assert attempt_id not in self.observations, "同一个 intent 不得重复启动进程"
        self.dispatches.append((attempt_id, fence, task, route))
        observation = WorkerObservation(attempt_id, fence, "running", session_id=f"s-{attempt_id}")
        self.observations[attempt_id] = observation
        if self.fail_after_dispatch:
            raise RuntimeError("启动调用断连，是否启动需要 reconciliation")
        return observation

    def poll(self, attempt_id: str, fence: int) -> WorkerObservation:
        self.polls.append((attempt_id, fence))
        return self.observations[attempt_id]

    def reconcile(self, attempt_id: str, fence: int) -> WorkerObservation:
        self.reconciliations.append((attempt_id, fence))
        return self.observations.get(attempt_id, WorkerObservation(attempt_id, fence, "unknown"))

    def cancel(self, attempt_id: str, fence: int) -> WorkerObservation:
        self.cancellations.append((attempt_id, fence))
        result = WorkerObservation(
            attempt_id, fence, "failed" if self.cancel_confirmed else "unknown",
            error_class="cancelled" if self.cancel_confirmed else None,
        )
        self.observations[attempt_id] = result
        return result

    def observe(self, task_id: str, state: str, **changes: Any) -> None:
        attempt_id, fence, _, _ = next(
            item for item in reversed(self.dispatches) if item[2].task_id == task_id
        )
        candidate = {"candidate_sha": SHA, "candidate_tree": TREE} if state == "candidate_complete" else {}
        self.observations[attempt_id] = WorkerObservation(
            attempt_id, fence, state, **(candidate | changes),
        )


class FakeVerificationExecutor:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.executions: list[tuple[VerificationPlan, Path]] = []
        self.receipt_changes: dict[str, Any] = {}
        self.returncode = 0
        self.after_execute: Any = None

    def execute(self, plan: VerificationPlan, *, workspace_path: Path) -> VerificationReceiptEnvelope:
        self.executions.append((plan, workspace_path))
        stamp = datetime.fromtimestamp(self.clock(), UTC).isoformat()
        receipt = VerificationReceiptEnvelope(
            f"receipt-{len(self.executions)}", plan.plan_id, plan.requirement_id, plan.task_id,
            plan.candidate_sha, plan.candidate_tree, dict(plan.environment),
            plan.commands_fingerprint,
            tuple(VerificationCommandResult(command.command_id, self.returncode, "c" * 64, "d" * 64)
                  for command in plan.commands),
            stamp, stamp, "fixture.executor", "1",
        )
        if self.after_execute is not None:
            self.after_execute()
        return replace(receipt, **self.receipt_changes)


class AlternatePlanner:
    """第二个可替换实现，不改变明确授权范围。"""

    def plan(self, request: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]:
        plan, decision = RulePlanningPolicy().plan(request)
        return plan, replace(decision, provider_id="fixture.alternate-planning")


class SplitPlanner:
    """真实地将一个目标转换为两个有依赖的子任务。"""

    def __init__(self, worktrees: tuple[Path, Path]) -> None:
        self.worktrees = worktrees

    def plan(self, request: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]:
        original = request.tasks[0]
        first = replace(original, task_id="split-a", title="子任务 A", worktree=str(self.worktrees[0]))
        second = replace(original, task_id="split-b", title="子任务 B", worktree=str(self.worktrees[1]),
                         depends_on=(first.task_id,))
        plan = ExecutionPlan("fixture-split", request.requirement_id, "dag", (first, second))
        return plan, PolicyDecision("fixture.split-planning", "1", "显式假件拆解",
                                    fingerprint(request.to_dict()), plan.to_dict())


class FabricatedRouter:
    def route(
        self, task: TaskSpec, runtimes: tuple[RuntimeDescriptor, ...],
    ) -> tuple[ModelRoute, PolicyDecision]:
        route = ModelRoute(runtimes[0].runtime_id, "invented-model", "high", "read-only")
        return route, PolicyDecision("fixture.bad-router", "1", "不可盲信插件输出",
                                     fingerprint(task.to_dict()), route.to_dict())


@dataclass
class Harness:
    root: Path
    clock: Clock
    store: OrchestrationStore
    protected: tuple[Path, ...]
    workers: FakeWorkers
    verifier: FakeVerificationExecutor
    candidates: dict[str, tuple[str, str]]

    def task(self, identifier: str = "T1", **changes: Any) -> TaskSpec:
        worktree = self.root / "tasks" / identifier
        worktree.mkdir(parents=True, exist_ok=True)
        return TaskSpec(identifier, identifier, "执行显式任务", worktree=str(worktree), **changes)

    def candidate(self, task: TaskSpec) -> tuple[str, str]:
        return self.candidates.get(task.task_id, (SHA, TREE))

    def supervisor(self, **changes: Any) -> RequirementSupervisor:
        options = {
            "owner": "controller-1", "workers": self.workers, "runtimes": (runtime(),),
            "protected_roots": self.protected, "clock": self.clock,
            "candidate_reader": self.candidate, "verification_executor": self.verifier,
        }
        options.update(changes)
        descriptors = options["runtimes"]
        if isinstance(descriptors, tuple):
            options["runtimes"] = lambda: descriptors
        return RequirementSupervisor(self.store, **options)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    clock = Clock()
    store = OrchestrationStore(tmp_path / "authority", clock=clock)
    protected = (store.root, tmp_path / "policy")
    for directory in (*protected, tmp_path / "tasks"):
        directory.mkdir()
    return Harness(tmp_path, clock, store, protected, FakeWorkers(store, protected),
                   FakeVerificationExecutor(clock), {})


def request(*tasks: TaskSpec) -> PlanningRequest:
    return PlanningRequest("REQ-020", "验证受控软件交付", tasks)


def node(supervisor: RequirementSupervisor, task_id: str = "T1") -> dict[str, Any]:
    return supervisor.status()["data"]["nodes"][task_id]


def tick_until(supervisor: RequirementSupervisor, task_id: str, statuses: set[str]) -> dict[str, Any]:
    for _ in range(6):
        result = supervisor.tick()
        if result["data"]["nodes"][task_id]["status"] in statuses:
            return result
    raise AssertionError(f"任务 {task_id} 未收敛至 {statuses}: {node(supervisor, task_id)}")


def candidate(harness: Harness, supervisor: RequirementSupervisor, task_id: str = "T1") -> None:
    harness.workers.observe(task_id, "candidate_complete")
    tick_until(supervisor, task_id, {"candidate_complete"})


@pytest.mark.parametrize("planner", [None, AlternatePlanner()])
def test_direct_candidate_requires_controlled_verification_and_never_completes_requirement(
    harness: Harness, planner: Any,
) -> None:
    supervisor = harness.supervisor(planner=planner)
    supervisor.acquire()
    initialized = supervisor.initialize(request(harness.task()))
    assert initialized["data"]["nodes"]["T1"]["status"] == "pending"
    tick_until(supervisor, "T1", {"running"})
    candidate(harness, supervisor)
    assert node(supervisor)["status"] == "candidate_complete"
    assert not harness.verifier.executions
    assert not hasattr(supervisor, "mark_accepted")
    verified = supervisor.verify_task("T1", COMMANDS, ENVIRONMENT)
    assert verified["data"]["nodes"]["T1"]["status"] == "accepted"
    assert verified["data"]["status"] == "ready_for_integration"
    assert len(harness.workers.dispatches) == len(harness.verifier.executions) == 1
    plan, cwd = harness.verifier.executions[0]
    assert (plan.candidate_sha, plan.candidate_tree) == (SHA, TREE)
    assert cwd == Path(node(supervisor)["spec"]["worktree"])


def test_dispatch_is_durable_and_repeated_ticks_do_not_launch_twice(harness: Harness) -> None:
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    tick_until(supervisor, "T1", {"running"})
    first = node(supervisor)
    for _ in range(4):
        supervisor.tick()
    assert len(harness.workers.dispatches) == 1
    assert node(supervisor)["active_attempt_id"] == first["active_attempt_id"]
    assert len(node(supervisor)["attempts"]) == 1


def test_dag_slots_and_dependencies_require_accepted_not_candidate(harness: Harness) -> None:
    supervisor = harness.supervisor(max_workers=1)
    supervisor.acquire()
    supervisor.initialize(request(harness.task("A"), harness.task("B", depends_on=("A",))))
    tick_until(supervisor, "A", {"running"})
    assert node(supervisor, "B")["status"] == "pending"
    candidate(harness, supervisor, "A")
    supervisor.tick()
    assert len(harness.workers.dispatches) == 1
    assert node(supervisor, "B")["status"] == "pending"
    supervisor.verify_task("A", COMMANDS, ENVIRONMENT)
    tick_until(supervisor, "B", {"running"})
    candidate(harness, supervisor, "B")
    result = supervisor.verify_task("B", COMMANDS, ENVIRONMENT)
    assert all(item["status"] == "accepted" for item in result["data"]["nodes"].values())
    assert result["data"]["status"] == "ready_for_integration"
    assert [item[2].task_id for item in harness.workers.dispatches] == ["A", "B"]


def test_parallel_ready_nodes_respect_global_slot_bound(harness: Harness) -> None:
    supervisor = harness.supervisor(max_workers=2)
    supervisor.acquire()
    supervisor.initialize(request(*(harness.task(name) for name in ("A", "B", "C"))))
    supervisor.tick()
    supervisor.tick()
    assert len(harness.workers.dispatches) == 2
    launched = [item[2].task_id for item in harness.workers.dispatches]
    waiting = next(name for name in ("A", "B", "C") if name not in launched)
    harness.workers.observe(launched[0], "unknown")
    supervisor.tick()
    supervisor.tick()
    assert len(harness.workers.dispatches) == 2
    assert node(supervisor, waiting)["status"] == "pending"


@pytest.mark.parametrize("change", [
    {"available": False}, {"models": ()},
    {"capabilities": ("start", "events", "profile:read-only")},
])
def test_runtime_missing_actual_capability_or_model_blocks_without_launch(
    harness: Harness, change: dict[str, Any],
) -> None:
    supervisor = harness.supervisor(runtimes=(runtime(**change),))
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    tick_until(supervisor, "T1", {"blocked"})
    assert not harness.workers.dispatches


def test_pluggable_router_cannot_invent_a_model(harness: Harness) -> None:
    supervisor = harness.supervisor(router=FabricatedRouter())
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    tick_until(supervisor, "T1", {"blocked"})
    assert not harness.workers.dispatches


@pytest.mark.parametrize("failure", ["unenforced", "writable-authority"])
def test_isolation_failure_blocks_without_dispatch(harness: Harness, failure: str) -> None:
    if failure == "unenforced":
        harness.workers.isolation_enforced = False
    else:
        harness.workers.extra_writable_root = harness.store.root
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(harness.task(write_required=True, branch="feat/T1")))
    tick_until(supervisor, "T1", {"blocked"})
    assert not harness.workers.dispatches


def test_all_mutating_operations_require_a_live_explicit_lease(harness: Harness) -> None:
    supervisor = harness.supervisor()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(harness.task()))
    assert harness.store.snapshot()["data"] == {}
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    harness.clock.advance(31)
    before = harness.store.snapshot()
    for operation in (supervisor.tick, supervisor.renew,
                      lambda: supervisor.verify_task("T1", COMMANDS, ENVIRONMENT)):
        with pytest.raises((WorkspaceError, ValueError)):
            operation()
    assert harness.store.snapshot() == before
    assert not harness.workers.dispatches


def test_crash_retry_budget_is_finite_and_attempt_history_is_preserved(harness: Harness) -> None:
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(harness.task(retry_budget=1)))
    tick_until(supervisor, "T1", {"running"})
    first_id = node(supervisor)["active_attempt_id"]
    harness.workers.observe("T1", "failed", error_class="crash")
    tick_until(supervisor, "T1", {"pending", "running"})
    tick_until(supervisor, "T1", {"running"})
    assert len(harness.workers.dispatches) == 2
    assert node(supervisor)["active_attempt_id"] != first_id
    harness.workers.observe("T1", "failed", error_class="crash")
    tick_until(supervisor, "T1", {"stopped"})
    for _ in range(3):
        supervisor.tick()
    assert len(harness.workers.dispatches) == 2
    assert len(node(supervisor)["attempts"]) == 2


def test_launch_disconnect_reconciles_existing_intent_without_duplicate_dispatch(harness: Harness) -> None:
    harness.workers.fail_after_dispatch = True
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    supervisor.tick()
    for _ in range(3):
        supervisor.tick()
    assert len(harness.workers.dispatches) == 1
    assert harness.workers.reconciliations
    assert node(supervisor)["status"] == "running"


def test_new_epoch_does_not_retry_until_old_worker_cancellation_is_confirmed(harness: Harness) -> None:
    first = harness.supervisor()
    first.acquire()
    first.initialize(request(harness.task(retry_budget=1)))
    tick_until(first, "T1", {"running"})
    harness.clock.advance(31)
    harness.workers.cancel_confirmed = False
    successor = harness.supervisor(owner="controller-2")
    successor.acquire()
    successor.tick()
    successor.tick()
    assert harness.workers.cancellations
    assert len(harness.workers.dispatches) == 1
    assert node(successor)["status"] in {"unknown", "blocked"}
    with pytest.raises((WorkspaceError, ValueError)):
        first.tick()


def test_new_epoch_confirmed_cancellation_can_consume_only_remaining_crash_budget(harness: Harness) -> None:
    first = harness.supervisor()
    first.acquire()
    first.initialize(request(harness.task(retry_budget=1)))
    tick_until(first, "T1", {"running"})
    old_attempt = node(first)["active_attempt_id"]
    harness.clock.advance(31)
    successor = harness.supervisor(owner="controller-2")
    successor.acquire()
    tick_until(successor, "T1", {"pending", "running"})
    tick_until(successor, "T1", {"running"})
    assert harness.workers.cancellations[0][0] == old_attempt
    assert len(harness.workers.dispatches) == 2
    assert harness.workers.dispatches[1][1] > harness.workers.dispatches[0][1]
    harness.workers.observe("T1", "failed", error_class="crash")
    tick_until(successor, "T1", {"stopped"})
    assert len(node(successor)["attempts"]) == 2


def test_human_blocked_worker_is_not_automatically_retried(harness: Harness) -> None:
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    tick_until(supervisor, "T1", {"running"})
    harness.workers.observe("T1", "blocked", summary="等待用户提供明确范围")
    tick_until(supervisor, "T1", {"blocked"})
    for _ in range(3):
        supervisor.tick()
    assert len(harness.workers.dispatches) == 1


def test_policy_can_split_one_task_only_with_explicit_authorized_roots(harness: Harness) -> None:
    source = harness.task("goal")
    worktrees = tuple(Path(harness.task(name).worktree) for name in ("child-a", "child-b"))
    supervisor = harness.supervisor(planner=SplitPlanner(worktrees),
                                    allowed_worktree_roots=(harness.root / "tasks",))
    supervisor.acquire()
    result = supervisor.initialize(request(source))
    assert set(result["data"]["nodes"]) == {"split-a", "split-b"}
    tick_until(supervisor, "split-a", {"running"})
    assert node(supervisor, "split-b")["status"] == "pending"
    candidate(harness, supervisor, "split-a")
    supervisor.verify_task("split-a", COMMANDS, ENVIRONMENT)
    tick_until(supervisor, "split-b", {"running"})


def test_empty_allowed_roots_cannot_expand_original_worktree(harness: Harness) -> None:
    source = harness.task("goal")
    worktrees = tuple(Path(harness.task(name).worktree) for name in ("child-a", "child-b"))
    supervisor = harness.supervisor(planner=SplitPlanner(worktrees))
    supervisor.acquire()
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(source))
    assert harness.store.snapshot() == before
    assert not harness.workers.dispatches


def prepare_candidate(harness: Harness, **changes: Any) -> RequirementSupervisor:
    supervisor = harness.supervisor(**changes)
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    tick_until(supervisor, "T1", {"running"})
    candidate(harness, supervisor)
    return supervisor


def verify_rejected(supervisor: RequirementSupervisor) -> None:
    try:
        supervisor.verify_task("T1", COMMANDS, ENVIRONMENT)
    except (WorkspaceError, ValueError):
        pass
    snapshot = supervisor.status()
    assert snapshot["data"]["nodes"]["T1"]["status"] != "accepted"
    assert snapshot["data"]["status"] != "ready_for_integration"


@pytest.mark.parametrize("changes", [
    {"plan_id": "unrelated-plan"}, {"requirement_id": "REQ-OTHER"}, {"task_id": "T-OTHER"},
    {"candidate_sha": "e" * 40}, {"candidate_tree": "e" * 40},
    {"environment": {"os": "other"}}, {"commands_fingerprint": "e" * 64},
    {"results": (VerificationCommandResult("unrequested-command", 0, "c" * 64, "d" * 64),)},
    {"started_at": "2000-01-01T00:00:00+00:00", "completed_at": "2000-01-01T00:00:00+00:00"},
    {"started_at": "2099-01-01T00:00:00+00:00", "completed_at": "2099-01-01T00:00:00+00:00"},
])
def test_verification_receipt_must_bind_exact_candidate_plan_commands_environment_and_time(
    harness: Harness, changes: dict[str, Any],
) -> None:
    supervisor = prepare_candidate(harness)
    harness.verifier.receipt_changes = changes
    verify_rejected(supervisor)
    assert len(harness.verifier.executions) == 1


def test_nonzero_verification_cannot_accept_candidate(harness: Harness) -> None:
    supervisor = prepare_candidate(harness)
    harness.verifier.returncode = 1
    verify_rejected(supervisor)


def test_worker_candidate_claim_is_checked_against_trusted_reader(harness: Harness) -> None:
    supervisor = prepare_candidate(harness)
    harness.candidates["T1"] = ("e" * 40, TREE)
    verify_rejected(supervisor)
    assert not harness.verifier.executions


def test_candidate_changed_during_verification_rejects_previous_sha_receipt(harness: Harness) -> None:
    supervisor = prepare_candidate(harness)
    harness.verifier.after_execute = lambda: harness.candidates.update({"T1": ("e" * 40, TREE)})
    verify_rejected(supervisor)
    assert len(harness.verifier.executions) == 1


def test_dirty_candidate_reader_cannot_authorize_verification(harness: Harness) -> None:
    def dirty_reader(task: TaskSpec) -> tuple[str, str]:
        raise WorkspaceError("候选工作区尚有未提交变更")

    supervisor = prepare_candidate(harness, candidate_reader=dirty_reader)
    verify_rejected(supervisor)
    assert not harness.verifier.executions


def test_missing_verification_executor_does_not_treat_worker_exit_as_pass(harness: Harness) -> None:
    supervisor = prepare_candidate(harness, verification_executor=None)
    verify_rejected(supervisor)
    assert not harness.verifier.executions


def test_expired_lease_during_verification_cannot_commit_acceptance(harness: Harness) -> None:
    supervisor = prepare_candidate(harness)
    harness.verifier.after_execute = lambda: harness.clock.advance(31)
    verify_rejected(supervisor)
    assert len(harness.verifier.executions) == 1


def test_old_fence_candidate_observation_is_not_business_completion(harness: Harness) -> None:
    supervisor = harness.supervisor()
    # 先释放第一次租约，取得 fence=2，才能构造格式合法但过期的 fence=1。
    supervisor.acquire()
    supervisor.close()
    successor = harness.supervisor(owner="controller-2")
    successor.acquire()
    successor.initialize(request(harness.task()))
    tick_until(successor, "T1", {"running"})
    attempt_id, fence, _, _ = harness.workers.dispatches[-1]
    harness.workers.observations[attempt_id] = WorkerObservation(
        attempt_id, fence - 1, "candidate_complete", candidate_sha=SHA, candidate_tree=TREE,
    )
    try:
        successor.tick()
    except (WorkspaceError, ValueError):
        pass
    assert node(successor)["status"] not in {"candidate_complete", "accepted"}
    assert len(harness.workers.dispatches) == 1
    assert not harness.verifier.executions


@pytest.mark.parametrize("write_required,retry_budget", [(True, 1), (False, 100)])
def test_plugin_planner_cannot_expand_write_or_retry_authority(
    harness: Harness, write_required: bool, retry_budget: int,
) -> None:
    class ExpandedPlanner:
        def plan(self, incoming: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]:
            enlarged = replace(incoming.tasks[0], write_required=write_required,
                               retry_budget=retry_budget, branch="feat/plugin-expanded")
            plan = ExecutionPlan("expanded", incoming.requirement_id, "direct", (enlarged,))
            return plan, PolicyDecision("fixture.expanded", "1", "越权的插件结果",
                                         fingerprint(incoming.to_dict()), plan.to_dict())

    supervisor = harness.supervisor(planner=ExpandedPlanner())
    supervisor.acquire()
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(harness.task(write_required=False, retry_budget=1)))
    assert harness.store.snapshot() == before
    assert not harness.workers.dispatches


def test_replan_cannot_rewrite_active_task_even_with_evidence(harness: Harness) -> None:
    original = harness.task()
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(original))
    tick_until(supervisor, "T1", {"running"})
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(replace(original, prompt="替换正在执行的任务")), evidence="用户补充范围")
    assert harness.store.snapshot() == before
    assert len(harness.workers.dispatches) == 1


def test_replan_cannot_rewrite_accepted_task(harness: Harness) -> None:
    supervisor = prepare_candidate(harness)
    supervisor.verify_task("T1", COMMANDS, ENVIRONMENT)
    before = harness.store.snapshot()
    specification = TaskSpec.from_dict(node(supervisor)["spec"])
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(replace(specification, prompt="覆盖已经通过的历史")), evidence="新描述")
    assert harness.store.snapshot() == before
    assert node(supervisor)["status"] == "accepted"


def test_replan_without_evidence_does_not_change_state(harness: Harness) -> None:
    specification = harness.task()
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(specification))
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(replace(specification, prompt="修改未执行任务")), evidence="")
    assert harness.store.snapshot() == before


def test_verification_failure_can_replan_once_without_erasing_attempt_history(harness: Harness) -> None:
    supervisor = prepare_candidate(harness, replan_budget=1)
    harness.verifier.returncode = 1
    verify_rejected(supervisor)
    assert node(supervisor)["status"] == "replan_required"
    previous = node(supervisor)
    original = TaskSpec.from_dict(previous["spec"])
    changed = replace(original, prompt="根据失败证据调整实现")
    revised = supervisor.replan(request(changed), evidence="unit 命令退出码 1，需调整实现")
    assert revised["data"]["nodes"]["T1"]["spec"]["prompt"] == changed.prompt
    assert revised["data"]["nodes"]["T1"]["attempts"] == previous["attempts"]
    assert revised["data"]["nodes"]["T1"]["revision"] > previous["revision"]
    tick_until(supervisor, "T1", {"running"})
    candidate(harness, supervisor)
    verify_rejected(supervisor)
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(replace(changed, prompt="再次重规划")), evidence="第二次验证失败")
    assert harness.store.snapshot() == before
    assert len(node(supervisor)["attempts"]) == 2
    assert len(node(supervisor)["verification_history"]) == 1
    assert node(supervisor)["verification_history"][0]["status"] == "failed"


class AlwaysRetry:
    def decide(self, context: RecoveryContext) -> tuple[RecoveryDecision, PolicyDecision]:
        result = RecoveryDecision("retry", "恶意插件尝试忽略预算与未知结果")
        return result, PolicyDecision("fixture.unsafe-recovery", "1", result.reason,
                                      fingerprint(context.to_dict()), result.to_dict())


def test_recovery_plugin_cannot_bypass_budget_or_unknown_execution(harness: Harness) -> None:
    supervisor = harness.supervisor(recovery=AlwaysRetry())
    supervisor.acquire()
    supervisor.initialize(request(harness.task(retry_budget=0)))
    supervisor.tick()
    harness.workers.observe("T1", "unknown")
    supervisor.tick()
    assert node(supervisor)["status"] == "unknown"
    assert node(supervisor)["active_attempt_id"] is not None
    assert len(harness.workers.dispatches) == 1
    harness.workers.observe("T1", "failed", error_class="crash")
    supervisor.tick()
    assert node(supervisor)["status"] == "stopped"
    for _ in range(2):
        supervisor.tick()
    assert len(harness.workers.dispatches) == 1


def test_router_cannot_mutate_its_input_to_fabricate_available_capability(harness: Harness) -> None:
    class MutatingRouter:
        def route(
            self, task: TaskSpec, runtimes: tuple[RuntimeDescriptor, ...]
        ) -> tuple[ModelRoute, PolicyDecision]:
            object.__setattr__(runtimes[0], "available", True)
            route = ModelRoute("fixture-runtime", "reported-model", "high", "read-only")
            return route, PolicyDecision("fixture.mutating-router", "1", "试图改写能力事实",
                                         fingerprint(task.to_dict()), route.to_dict())

    descriptor = runtime(available=False)
    supervisor = harness.supervisor(router=MutatingRouter(), runtimes=(descriptor,))
    supervisor.acquire()
    supervisor.initialize(request(harness.task()))
    supervisor.tick()
    assert descriptor.available is False
    assert node(supervisor)["status"] == "blocked"
    assert harness.workers.dispatches == []


def test_executor_exception_keeps_unknown_verification_frozen_and_workspace_reserved(harness: Harness) -> None:
    class UncertainExecutor:
        def execute(self, plan: VerificationPlan, *, workspace_path: Path) -> VerificationReceiptEnvelope:
            raise RuntimeError("Executor 连接丢失，无法确认子进程终止")

    task = harness.task()
    other = replace(harness.task("T2"), worktree=task.worktree)
    supervisor = harness.supervisor(verification_executor=UncertainExecutor())
    supervisor.acquire()
    supervisor.initialize(request(task, other))
    supervisor.tick()
    candidate(harness, supervisor)
    supervisor.verify_task("T1", COMMANDS, ENVIRONMENT)
    assert node(supervisor)["verification"]["status"] == "unknown"
    assert node(supervisor)["status"] == "blocked"
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(replace(task, prompt="不能绕过未知验证"), other), evidence="尝试重规划")
    assert harness.store.snapshot() == before
    supervisor.tick()
    assert len(harness.workers.dispatches) == 1
    assert node(supervisor, "T2")["status"] == "pending"


class SourcePlanner:
    """保留审计绑定，可构造合法拆解与有意越权的策略输出。"""

    def __init__(self, transform: Any) -> None:
        self.transform = transform

    def plan(self, incoming: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]:
        tasks = tuple(self.transform(incoming))
        plan = ExecutionPlan("source-" + fingerprint([task.to_dict() for task in tasks]),
                             incoming.requirement_id, "direct" if len(tasks) == 1 else "dag", tasks)
        return plan, PolicyDecision("fixture.source-planner", "1", "受审计的任务来源分解",
                                     fingerprint(incoming.to_dict()), plan.to_dict())


def mixed_sources(harness: Harness) -> tuple[TaskSpec, TaskSpec]:
    return (harness.task("readonly", retry_budget=0),
            harness.task("writer", write_required=True, branch="feat/writer", retry_budget=2))


@pytest.mark.parametrize("changes", [
    {"write_required": True, "branch": "feat/elevated"}, {"retry_budget": 2},
    {"write_required": True, "branch": "feat/elevated", "retry_budget": 2},
    {"source_task_ids": ("writer",)},
])
def test_mixed_original_tasks_cannot_borrow_write_permission_or_retry_budget(
    harness: Harness, changes: dict[str, Any]
) -> None:
    readonly, writer = mixed_sources(harness)
    planner = SourcePlanner(lambda incoming: (replace(incoming.tasks[0], **changes), incoming.tasks[1]))
    supervisor = harness.supervisor(planner=planner, max_workers=2)
    supervisor.acquire()
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(readonly, writer))
    assert harness.store.snapshot() == before
    assert harness.workers.dispatches == []


@pytest.mark.parametrize("field,value", [
    ("preferred_runtime", "fixture-runtime"), ("preferred_model", "reported-model"),
    ("preferred_effort", "high"),
])
def test_default_planner_preserves_mixed_explicit_and_default_preferences(
    harness: Harness, field: str, value: str
) -> None:
    explicit = harness.task("explicit", **{field: value})
    default = harness.task("default")
    supervisor = harness.supervisor(max_workers=2)
    supervisor.acquire()
    supervisor.initialize(request(explicit, default))
    supervisor.tick()
    assert node(supervisor, "explicit")["spec"][field] == value
    assert node(supervisor, "default")["spec"][field] is None
    assert len(harness.workers.dispatches) == 2


def test_default_planner_handles_distinct_preferences_without_global_inheritance(harness: Harness) -> None:
    first = harness.task("first", preferred_runtime="fixture-runtime", preferred_effort="low")
    second = harness.task("second", preferred_runtime="other-runtime", preferred_effort="high")
    default = harness.task("default")
    descriptors = (runtime(), replace(runtime(), runtime_id="other-runtime"))
    supervisor = harness.supervisor(max_workers=3, runtimes=descriptors)
    supervisor.acquire()
    supervisor.initialize(request(first, second, default))
    supervisor.tick()
    routed = {task.task_id: route for _, _, task, route in harness.workers.dispatches}
    assert routed["first"].runtime_id == "fixture-runtime" and routed["first"].effort == "low"
    assert routed["second"].runtime_id == "other-runtime" and routed["second"].effort == "high"
    assert node(supervisor, "default")["spec"]["preferred_runtime"] is None
    assert len(routed) == 3


def test_mixed_task_retry_limits_remain_per_source_after_restore(harness: Harness) -> None:
    readonly, writer = mixed_sources(harness)
    supervisor = harness.supervisor(max_workers=2, recovery=AlwaysRetry())
    supervisor.acquire()
    supervisor.initialize(request(readonly, writer))
    supervisor.tick()
    harness.workers.observe("readonly", "failed", error_class="crash")
    harness.workers.observe("writer", "failed", error_class="crash")
    supervisor.tick()
    assert node(supervisor, "readonly")["status"] == "stopped"
    assert node(supervisor, "readonly")["retry_count"] == 0
    assert len(node(supervisor, "readonly")["attempts"]) == 1
    assert len(node(supervisor, "writer")["attempts"]) == 2
    supervisor.close()
    restored = RequirementSupervisor(
        OrchestrationStore(harness.store.root, clock=harness.clock), owner="restored",
        workers=harness.workers, runtimes=lambda: (runtime(),), clock=harness.clock,
        protected_roots=harness.protected, max_workers=2,
    )
    state = restored.status()
    assert state["data"]["nodes"]["readonly"]["spec"]["retry_budget"] == 0
    assert state["data"]["nodes"]["writer"]["spec"]["retry_budget"] == 2
    assert state["data"]["nodes"]["readonly"]["status"] == "stopped"


@pytest.mark.parametrize("changes", [{"write_required": True, "branch": "feat/elevated"}, {"retry_budget": 2}])
def test_restore_revalidates_mixed_authority_even_with_consistent_forged_plan_history(
    harness: Harness, changes: dict[str, Any]
) -> None:
    readonly, writer = mixed_sources(harness)
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(readonly, writer))
    supervisor.close()
    path = harness.store.root / "state.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    data = envelope["data"]
    elevated = replace(readonly, **changes).to_dict()
    data["plan"]["nodes"][0] = elevated
    data["nodes"]["readonly"]["spec"] = elevated
    data["plans"][0]["plan"] = data["plan"]
    data["plans"][0]["decision"]["decision"] = data["plan"]
    data["decisions"][0]["policy"]["decision"] = data["plan"]
    raw = json.dumps(envelope).encode("utf-8")
    path.write_bytes(raw)
    restored = harness.supervisor(owner="restored")
    with pytest.raises((WorkspaceError, ValueError)):
        restored.status()
    assert path.read_bytes() == raw


@pytest.mark.parametrize("sources", [(), ("missing",)])
def test_multiple_initial_tasks_require_explicit_valid_derived_source(
    harness: Harness, sources: tuple[str, ...]
) -> None:
    readonly, writer = mixed_sources(harness)
    child = harness.task("child", source_task_ids=sources, retry_budget=0)
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (child,)),
                                     allowed_worktree_roots=(harness.root / "tasks",))
    supervisor.acquire()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(readonly, writer))
    assert harness.store.snapshot()["data"] == {}


@pytest.mark.parametrize("sources", [("readonly",), ("readonly", "writer")])
@pytest.mark.parametrize("changes", [
    {"write_required": True, "branch": "feat/child"}, {"retry_budget": 1},
])
def test_derived_readonly_and_combined_sources_use_intersection_not_union(
    harness: Harness, changes: dict[str, Any], sources: tuple[str, ...]
) -> None:
    readonly, writer = mixed_sources(harness)
    child = harness.task("child", source_task_ids=sources, retry_budget=0)
    child = replace(child, **changes)
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (child,)),
                                     allowed_worktree_roots=(harness.root / "tasks",))
    supervisor.acquire()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(readonly, writer))
    assert harness.workers.dispatches == []


def test_legal_combined_sources_keep_readonly_and_minimum_retry(harness: Harness) -> None:
    readonly, writer = mixed_sources(harness)
    child = harness.task("combined", source_task_ids=("readonly", "writer"), retry_budget=0)
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (child,)),
                                     allowed_worktree_roots=(harness.root / "tasks",))
    supervisor.acquire()
    supervisor.initialize(request(readonly, writer))
    supervisor.tick()
    executed = harness.workers.dispatches[0]
    assert executed[2].source_task_ids == ("readonly", "writer")
    assert executed[2].retry_budget == 0 and executed[3].sandbox == "read-only"


@pytest.mark.parametrize("explicit_roots", [False, True])
@pytest.mark.parametrize("rename", [False, True])
def test_source_cannot_take_another_original_workspace_even_with_shared_allocation_pool(
    harness: Harness, explicit_roots: bool, rename: bool
) -> None:
    readonly, writer = mixed_sources(harness)
    moved = replace(readonly, task_id="child" if rename else readonly.task_id,
                    source_task_ids=("readonly",) if rename else (), worktree=writer.worktree)
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (moved,)),
                                     allowed_worktree_roots=(harness.root / "tasks",) if explicit_roots else ())
    supervisor.acquire()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(readonly, writer))
    assert harness.workers.dispatches == []


@pytest.mark.parametrize("retire_first", [False, True])
def test_replan_cannot_rebind_derived_identity_to_more_privileged_source(
    harness: Harness, retire_first: bool
) -> None:
    readonly, writer = mixed_sources(harness)
    child = harness.task("child", source_task_ids=("readonly",), retry_budget=0)
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (child,)),
                                     allowed_worktree_roots=(harness.root / "tasks",), replan_budget=2)
    supervisor.acquire()
    supervisor.initialize(request(readonly, writer))
    supervisor.planner = RulePlanningPolicy()
    if retire_first:
        supervisor.replan(request(writer), evidence="保留历史，移除未执行派生节点")
        assert node(supervisor, "child")["status"] == "retired"
    rebound = replace(child, source_task_ids=("writer",), write_required=True,
                      branch="feat/borrowed", retry_budget=2)
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(rebound, writer), evidence="不能用 replan 转移授权")
    assert harness.store.snapshot() == before


def test_replan_cannot_steal_former_derived_directory_after_original_child_moves(harness: Harness) -> None:
    readonly, writer = mixed_sources(harness)
    child = harness.task("child", source_task_ids=("readonly",), retry_budget=0)
    moved_root = Path(harness.task("new-child-root").worktree or "")
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (child,)),
                                     allowed_worktree_roots=(harness.root / "tasks",), replan_budget=2)
    supervisor.acquire()
    supervisor.initialize(request(readonly, writer))
    supervisor.planner = RulePlanningPolicy()
    moved = replace(child, worktree=str(moved_root))
    supervisor.replan(request(moved), evidence="明确为同一来源重分配目录，历史仍保留")
    stolen = replace(writer, task_id="other-child", source_task_ids=("writer",), worktree=child.worktree)
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(moved, stolen), evidence="不能占用另一来源历史目录")
    assert harness.store.snapshot() == before


@pytest.mark.parametrize("field,value", [
    ("preferred_runtime", "fixture-runtime"), ("preferred_model", "reported-model"),
    ("preferred_effort", "high"),
])
def test_replan_cannot_convert_existing_default_preference_into_another_tasks_constraint(
    harness: Harness, field: str, value: str
) -> None:
    explicit = harness.task("explicit", **{field: value})
    default = harness.task("default")
    supervisor = harness.supervisor()
    supervisor.acquire()
    supervisor.initialize(request(explicit, default))
    before = harness.store.snapshot()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.replan(request(explicit, replace(default, **{field: value})), evidence="插件不能改写默认选择")
    assert harness.store.snapshot() == before


def test_conflicting_source_preferences_cannot_be_silently_merged(harness: Harness) -> None:
    first = harness.task("first", preferred_runtime="fixture-runtime")
    second = harness.task("second", preferred_runtime="other-runtime")
    child = harness.task("child", source_task_ids=("first", "second"), preferred_runtime="fixture-runtime")
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (child,)),
                                     allowed_worktree_roots=(harness.root / "tasks",))
    supervisor.acquire()
    with pytest.raises((WorkspaceError, ValueError), match="偏好"):
        supervisor.initialize(request(first, second))


def test_professional_planner_can_split_one_source_in_mixed_request_into_real_dag(harness: Harness) -> None:
    source = harness.task("source", write_required=True, branch="feat/source", retry_budget=1,
                          preferred_runtime="fixture-runtime", preferred_model="reported-model")
    default = harness.task("default", retry_budget=0)
    first_root = Path(harness.task("step-1").worktree or "")
    second_root = Path(harness.task("step-2").worktree or "")
    first = replace(source, task_id="step-1", worktree=str(first_root), source_task_ids=("source",))
    second = replace(source, task_id="step-2", worktree=str(second_root), source_task_ids=("source",),
                     depends_on=("step-1",))
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (first, second, default)),
                                     allowed_worktree_roots=(harness.root / "tasks",), max_workers=2)
    supervisor.acquire()
    supervisor.initialize(request(source, default))
    supervisor.tick()
    assert {item[2].task_id for item in harness.workers.dispatches} == {"step-1", "default"}
    candidate(harness, supervisor, "step-1")
    supervisor.verify_task("step-1", COMMANDS, ENVIRONMENT)
    supervisor.tick()
    assert {item[2].task_id for item in harness.workers.dispatches} == {"step-1", "step-2", "default"}
    for task_id in ("step-2", "default"):
        candidate(harness, supervisor, task_id)
        supervisor.verify_task(task_id, COMMANDS, ENVIRONMENT)
    assert supervisor.status()["data"]["status"] == "ready_for_integration"
    assert node(supervisor, "default")["spec"]["preferred_runtime"] is None
    assert node(supervisor, "step-2")["spec"]["source_task_ids"] == ["source"]


def test_derived_default_source_does_not_inherit_an_unrelated_tasks_preference(harness: Harness) -> None:
    explicit = harness.task("explicit", preferred_runtime="fixture-runtime", preferred_effort="low")
    default = harness.task("default")
    child = harness.task("default-child", source_task_ids=("default",))
    supervisor = harness.supervisor(planner=SourcePlanner(lambda incoming: (explicit, child)),
                                     allowed_worktree_roots=(harness.root / "tasks",), max_workers=2)
    supervisor.acquire()
    supervisor.initialize(request(explicit, default))
    supervisor.tick()
    assert len(harness.workers.dispatches) == 2
    assert node(supervisor, "default-child")["spec"]["preferred_runtime"] is None
    assert node(supervisor, "default-child")["spec"]["preferred_effort"] is None


@pytest.mark.parametrize("widen", [False, True])
def test_explicit_nested_source_workspaces_keep_only_their_original_shared_scope(
    harness: Harness, widen: bool
) -> None:
    parent = harness.task("parent", write_required=True, branch="feat/parent")
    nested_path = Path(parent.worktree or "") / "nested"
    nested_path.mkdir()
    nested = replace(harness.task("nested"), worktree=str(nested_path))
    planner = SourcePlanner(lambda incoming: (
        replace(nested, worktree=parent.worktree) if widen else nested, parent,
    ))
    supervisor = harness.supervisor(planner=planner, allowed_worktree_roots=(harness.root / "tasks",), max_workers=2)
    supervisor.acquire()
    if widen:
        with pytest.raises((WorkspaceError, ValueError)):
            supervisor.initialize(request(nested, parent))
        assert harness.workers.dispatches == []
    else:
        supervisor.initialize(request(nested, parent))
        supervisor.tick()
        assert len(harness.workers.dispatches) == 1
        assert harness.workers.dispatches[0][2].task_id == "nested"


def test_execution_claim_receives_checked_copies_before_plan_commit_and_dispatch(harness: Harness) -> None:
    calls: list[tuple[str, str]] = []

    def claim(initial: PlanningRequest, plan: ExecutionPlan) -> None:
        calls.append((initial.requirement_id, plan.plan_id))
        if len(calls) == 1:
            assert harness.store.snapshot()["data"] == {}
        object.__setattr__(plan.nodes[0], "write_required", True)

    task = harness.task()
    supervisor = harness.supervisor(execution_claim=claim)
    supervisor.acquire()
    supervisor.initialize(request(task))
    assert len(calls) == 1
    assert node(supervisor)["spec"]["write_required"] is False
    supervisor.initialize(request(task))
    assert len(calls) == 2
    supervisor.tick()
    assert len(calls) >= 3
    assert len(harness.workers.dispatches) == 1
    assert harness.workers.dispatches[0][2].write_required is False


def test_execution_claim_failure_keeps_uninitialized_plan_and_resumed_dispatch_closed(harness: Harness) -> None:
    def unavailable(initial: PlanningRequest, plan: ExecutionPlan) -> None:
        raise WorkspaceError("可信执行归属不可用")

    task = harness.task()
    supervisor = harness.supervisor(execution_claim=unavailable)
    supervisor.acquire()
    with pytest.raises(WorkspaceError, match="执行归属"):
        supervisor.initialize(request(task))
    assert harness.store.snapshot()["data"] == {}
    supervisor.execution_claim = None
    supervisor.initialize(request(task))
    supervisor.close()
    restored = harness.supervisor(owner="restored", execution_claim=unavailable)
    restored.acquire()
    with pytest.raises(WorkspaceError, match="执行归属"):
        restored.tick()
    assert harness.workers.dispatches == []
    assert node(restored)["status"] == "pending"


def test_rejected_source_plan_never_claims_execution_ownership(harness: Harness) -> None:
    calls: list[str] = []
    readonly, writer = mixed_sources(harness)
    supervisor = harness.supervisor(
        planner=SourcePlanner(lambda incoming: (replace(readonly, retry_budget=2), writer)),
        execution_claim=lambda initial, plan: calls.append(plan.plan_id),
    )
    supervisor.acquire()
    with pytest.raises((WorkspaceError, ValueError)):
        supervisor.initialize(request(readonly, writer))
    assert calls == []
