"""结构化策略、真实模型路由、有限恢复与验证证据契约；无外部副作用。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from workspace_orchestrator.agent_runtime.contracts import ModelDescriptor, RuntimeDescriptor
from workspace_orchestrator.orchestration.contracts import (
    ExecutionPlan,
    ModelRoute,
    PlanningRequest,
    PolicyDecision,
    PolicyError,
    RecoveryContext,
    TaskSpec,
    VerificationCommand,
    VerificationCommandResult,
    VerificationPlan,
    VerificationPlanningRequest,
    VerificationReceiptEnvelope,
    WorkerIsolation,
    WorkerObservation,
    commands_fingerprint,
    fingerprint,
)
from workspace_orchestrator.orchestration.policies import (
    BoundedRecoveryPolicy,
    CapabilityModelRouter,
    RulePlanningPolicy,
    RuleVerificationPlanner,
    validate_route,
)
from workspace_orchestrator.orchestration.ports import PlanningPolicy


def task(identifier: str = "T1", **kwargs: Any) -> TaskSpec:
    return TaskSpec(identifier, identifier, "执行显式任务", **kwargs)


def runtime(
    identifier: str = "runtime-a", *, available: bool = True,
    capabilities: tuple[str, ...] = ("start", "message", "events", "profile:read-only"),
) -> RuntimeDescriptor:
    return RuntimeDescriptor(identifier, identifier, "fixture-1", available, capabilities, (
        ModelDescriptor("discovered-a", "Model A", ("low", "medium", "high"), True),
        ModelDescriptor("discovered-b", "Model B", ("low", "high", "ultra")),
    ))


def verification_request() -> VerificationPlanningRequest:
    return VerificationPlanningRequest(
        "REQ-020", "T1", "a" * 40, "b" * 40, {"os": "hermetic", "python": "fixture"},
        (VerificationCommand("unit", ("python", "-m", "pytest"), 30),
         VerificationCommand("lint", ("python", "-m", "ruff", "check", "."), 30)),
    )


def receipt(plan: VerificationPlan, **changes: Any) -> VerificationReceiptEnvelope:
    value = VerificationReceiptEnvelope(
        "receipt-1", plan.plan_id, plan.requirement_id, plan.task_id, plan.candidate_sha,
        plan.candidate_tree, dict(plan.environment), plan.commands_fingerprint,
        tuple(VerificationCommandResult(command.command_id, 0, "c" * 64, "d" * 64)
              for command in plan.commands),
        "2026-09-05T12:00:00+00:00", "2026-09-05T12:01:00+00:00", "fixture.executor", "1",
    )
    return replace(value, **changes)


def test_rule_planning_direct_dag_and_repeated_inputs_are_stable() -> None:
    planner = RulePlanningPolicy()
    single = PlanningRequest("REQ-020", "用户明确目标", (task(),))
    plan, decision = planner.plan(single)
    assert plan.mode == "direct" and plan.nodes == single.tasks
    assert decision.input_fingerprint == fingerprint(single.to_dict())
    assert decision.decision == plan.to_dict()
    assert planner.plan(single) == (plan, decision)
    multiple = replace(single, tasks=(task(), task("T2", depends_on=("T1",))))
    graph, record = planner.plan(multiple)
    assert graph.mode == "dag" and graph.nodes == multiple.tasks
    assert record.input_fingerprint != decision.input_fingerprint


@pytest.mark.parametrize("tasks", [
    (), (task(), task()),
    (task(depends_on=("missing",)),),
    (task(depends_on=("T2",)), task("T2", depends_on=("T1",))),
])
def test_invalid_graphs_fail_before_dispatch(tasks: tuple[TaskSpec, ...]) -> None:
    with pytest.raises(PolicyError, match="非空|重复|未知|依赖环"):
        PlanningRequest("REQ-020", "goal", tasks)


def test_direct_cannot_hide_multiple_tasks_and_task_limits_are_strict() -> None:
    with pytest.raises(PolicyError, match="direct"):
        ExecutionPlan("p", "REQ-020", "direct", (task(), task("T2")))
    with pytest.raises(PolicyError, match="自己"):
        task(depends_on=("T1",))
    for budget in (-1, True, 1.5):
        with pytest.raises(PolicyError):
            task(retry_budget=budget)


def test_contract_roundtrip_preserves_unknown_nested_extensions() -> None:
    original = ExecutionPlan("p", "REQ-020", "direct", (
        task(extra={"future_task": {"nested": [1, 2]}}),
    ), extra={"future_plan": {"flag": True}})
    serialized = json.loads(json.dumps(original.to_dict()))
    loaded = ExecutionPlan.from_dict(serialized)
    assert loaded == original
    assert loaded.to_dict() == original.to_dict()
    request = PlanningRequest("REQ-020", "goal", original.nodes)
    assert PlanningRequest.from_dict(json.loads(json.dumps(request.to_dict()))) == request
    with pytest.raises(PolicyError, match="schema_version"):
        ExecutionPlan.from_dict({key: value for key, value in serialized.items() if key != "schema_version"})
    with pytest.raises(PolicyError, match="schema"):
        ExecutionPlan.from_dict({**serialized, "schema_version": True})


def test_optional_task_sources_preserve_legacy_documents_and_policy_fingerprints() -> None:
    original = task(extra={"future": {"keep": [True, None]}})
    document = original.to_dict()
    assert "source_task_ids" not in document
    loaded = TaskSpec.from_dict(json.loads(json.dumps(document)))
    assert loaded.source_task_ids == ()
    assert loaded.to_dict() == document
    assert fingerprint(loaded.to_dict()) == fingerprint(document)
    request = PlanningRequest("REQ-020", "兼容旧计划", (loaded,))
    plan, decision = RulePlanningPolicy().plan(request)
    saved = json.loads(json.dumps(plan.to_dict()))
    assert ExecutionPlan.from_dict(saved).to_dict() == saved
    assert decision.input_fingerprint == fingerprint(request.to_dict())
    assert decision.decision == saved
    explicit_empty = TaskSpec.from_dict({**document, "source_task_ids": []})
    assert explicit_empty == original and explicit_empty.to_dict() == document


def test_explicit_task_sources_roundtrip_without_becoming_dag_dependencies() -> None:
    derived = task("child", source_task_ids=("original-a", "original-b"),
                   extra={"future": {"keep": [1, 2]}})
    document = derived.to_dict()
    assert document["source_task_ids"] == ["original-a", "original-b"]
    assert TaskSpec.from_dict(json.loads(json.dumps(document))) == derived
    plan = ExecutionPlan("derived", "REQ-020", "direct", (derived,))
    assert ExecutionPlan.from_dict(plan.to_dict()) == plan
    assert derived.depends_on == ()


@pytest.mark.parametrize("sources", [None, "A", ["A"], ("A", "A"), ("",), (True,), ("A\n",)])
def test_task_sources_require_unique_nonempty_string_tuple(sources: Any) -> None:
    with pytest.raises(PolicyError, match="source_task_ids"):
        task(source_task_ids=sources)


@pytest.mark.parametrize("sources", [None, "A", ["A", "A"], [1], [""]])
def test_decoded_task_sources_reject_ambiguous_authority(sources: Any) -> None:
    with pytest.raises(PolicyError, match="source_task_ids"):
        TaskSpec.from_dict({**task().to_dict(), "source_task_ids": sources})


@pytest.mark.parametrize("complexity,effort", [("tiny", "low"), ("normal", "medium"), ("complex", "high")])
def test_routing_uses_discovered_default_model_and_available_efforts(complexity: str, effort: str) -> None:
    specification = task(complexity=complexity)
    descriptors = (runtime(),)
    route, decision = CapabilityModelRouter().route(specification, descriptors)
    assert route == ModelRoute("runtime-a", "discovered-a", effort, "read-only")
    validate_route(specification, route, descriptors)
    assert decision.decision == route.to_dict()


def test_router_honors_explicit_preferences_and_never_falls_back_silently() -> None:
    specification = task(preferred_runtime="runtime-b", preferred_model="discovered-b", preferred_effort="ultra")
    descriptors = (runtime(), runtime("runtime-b"))
    route, _ = CapabilityModelRouter().route(specification, descriptors)
    assert (route.runtime_id, route.model, route.effort) == ("runtime-b", "discovered-b", "ultra")
    for changes in ({"preferred_runtime": "missing"}, {"preferred_model": "invented"},
                    {"preferred_effort": "unsupported"}):
        with pytest.raises(PolicyError) as captured:
            CapabilityModelRouter().route(replace(specification, **changes), descriptors)
        assert captured.value.code == "no_route"


@pytest.mark.parametrize("descriptors", [
    (), (runtime(available=False),),
    (replace(runtime(), models=()),),
    (runtime(capabilities=("start", "message", "events")),),
    (runtime(capabilities=("start", "events", "profile:read-only")),),
])
def test_no_usable_runtime_model_or_profile_is_not_faked(descriptors: tuple[RuntimeDescriptor, ...]) -> None:
    with pytest.raises(PolicyError) as captured:
        CapabilityModelRouter().route(task(), descriptors)
    assert captured.value.code == "no_route"


def test_write_task_cannot_be_routed_to_read_only_or_unconfined_profile() -> None:
    specification = task(write_required=True, worktree="/isolated/T1", branch="feat/T1")
    with pytest.raises(PolicyError):
        CapabilityModelRouter().route(specification, (runtime(),))
    capable = runtime(capabilities=("start", "message", "events", "profile:workspace-write"))
    route, _ = CapabilityModelRouter().route(specification, (capable,))
    assert route.sandbox == "workspace-write"
    with pytest.raises(PolicyError):
        validate_route(task(), route, (capable,))
    with pytest.raises(PolicyError):
        ModelRoute("runtime-a", "discovered-a", "low", "danger-full-access")


def test_unknown_effort_preserves_provider_default_and_duplicate_facts_fail_closed() -> None:
    descriptor = replace(runtime(), models=(ModelDescriptor("new-model", "New", ("new-effort",)),))
    route, _ = CapabilityModelRouter().route(task(complexity="complex"), (descriptor,))
    assert route.effort is None
    with pytest.raises(PolicyError):
        CapabilityModelRouter().route(task(), (runtime(), runtime()))
    duplicate = replace(runtime(), models=(runtime().models[0], runtime().models[0]))
    with pytest.raises(PolicyError):
        CapabilityModelRouter().route(task(), (duplicate,))


def test_replaced_router_output_must_still_satisfy_core_validation() -> None:
    descriptors = (runtime(),)
    for route in (ModelRoute("missing", "discovered-a", "low", "read-only"),
                  ModelRoute("runtime-a", "not-reported", "low", "read-only"),
                  ModelRoute("runtime-a", "discovered-a", "not-reported", "read-only")):
        with pytest.raises(PolicyError):
            validate_route(task(), route, descriptors)


def test_verification_plan_receipt_bind_exact_candidate_tree_commands_and_environment() -> None:
    planner = RuleVerificationPlanner()
    plan, decision = planner.plan(verification_request())
    assert decision.decision == plan.to_dict()
    assert plan.commands_fingerprint == commands_fingerprint(plan.commands)
    verified = receipt(plan)
    verified.validate_for(plan)
    assert VerificationReceiptEnvelope.from_dict(json.loads(json.dumps(verified.to_dict()))) == verified
    assert VerificationPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan
    for changes in ({"candidate_sha": "e" * 40}, {"candidate_tree": "e" * 40},
                    {"environment": {"os": "different"}}, {"commands_fingerprint": "e" * 64},
                    {"task_id": "other"}, {"plan_id": "other"}):
        with pytest.raises(PolicyError) as captured:
            receipt(plan, **changes).validate_for(plan)
        assert captured.value.code == "stale_verification"


def test_receipt_cannot_omit_reorder_or_fail_commands() -> None:
    plan, _ = RuleVerificationPlanner().plan(verification_request())
    verified = receipt(plan)
    for results in (verified.results[:1], tuple(reversed(verified.results)),
                    (replace(verified.results[0], returncode=1), verified.results[1])):
        invalid = replace(verified, results=results)
        invalid.validate()  # 可读取失败证据，但不能把它当作 PASS。
        with pytest.raises(PolicyError):
            invalid.validate_for(plan)


def test_verification_rejects_missing_candidate_empty_commands_and_forged_fingerprint() -> None:
    request = verification_request()
    for changes in ({"candidate_sha": "main"}, {"candidate_tree": ""}, {"commands": ()},
                    {"environment": {}}, {"commands": (request.commands[0], request.commands[0])}):
        with pytest.raises(PolicyError):
            replace(request, **changes)
    plan, _ = RuleVerificationPlanner().plan(request)
    with pytest.raises(PolicyError):
        replace(plan, commands_fingerprint="f" * 64)
    for timeout in (0, -1, True):
        with pytest.raises(PolicyError):
            VerificationCommand("id", ("python",), timeout)
    with pytest.raises(PolicyError):
        receipt(plan, completed_at="2020-01-01T00:00:00+00:00")


@pytest.mark.parametrize("error", ["crash", "timeout", "provider_offline", "unavailable"])
def test_recovery_budget_is_finite_and_duplicate_risk_prevents_retry(error: str) -> None:
    policy = BoundedRecoveryPolicy()
    before = RecoveryContext(error, attempts=0, retry_budget=1)
    assert policy.decide(before)[0].action == "retry"
    assert policy.decide(replace(before, attempts=1))[0].action == "stop"
    assert policy.decide(replace(before, duplicate_risk=True))[0].action == "escalate"


def test_recovery_replan_and_unknown_cases_converge() -> None:
    policy = BoundedRecoveryPolicy()
    context = RecoveryContext("verification_fail", 0, 1)
    decision, evidence = policy.decide(context)
    assert decision.action == "replan" and evidence.decision == decision.to_dict()
    assert policy.decide(replace(context, replan_count=1))[0].action == "escalate"
    for error in ("unknown", "ambiguous_result", "lease_lost", "brand_new_error"):
        assert policy.decide(replace(context, error_class=error))[0].action == "escalate"
    for error in ("cancelled", "policy_violation", "isolation_failure", "invalid_contract"):
        assert policy.decide(replace(context, error_class=error))[0].action == "stop"


def test_worker_observations_cannot_claim_authoritative_completion() -> None:
    for state in ("done", "accepted", "completed"):
        with pytest.raises(PolicyError):
            WorkerObservation("attempt", 1, state)  # type: ignore[arg-type]
    with pytest.raises(PolicyError):
        WorkerObservation("attempt", 1, "candidate_complete")
    with pytest.raises(PolicyError):
        WorkerObservation("attempt", 0, "running")
    candidate = WorkerObservation("attempt", 1, "candidate_complete",
                                  candidate_sha="a" * 40, candidate_tree="b" * 40, summary="待验证")
    assert WorkerObservation.from_dict(candidate.to_dict()) == candidate
    isolation = WorkerIsolation("unavailable", False, (), (), "本机未配置受信隔离")
    assert not WorkerIsolation.from_dict(isolation.to_dict()).enforced


class ReorderedFakePlanner:
    def plan(self, request: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]:
        output = ExecutionPlan("fake-reordered", request.requirement_id, "dag", tuple(reversed(request.tasks)))
        return output, PolicyDecision("fixture.reordered", "1", "仅重排节点不修改依赖",
                                      fingerprint(request.to_dict()), output.to_dict())


class DirectAwareFakePlanner:
    def plan(self, request: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]:
        output = ExecutionPlan("fake-direct-aware", request.requirement_id,
                               "direct" if len(request.tasks) == 1 else "dag", request.tasks)
        return output, PolicyDecision("fixture.direct-aware", "2", "替换实现独立选择执行形态",
                                      fingerprint(request.to_dict()), output.to_dict())


@pytest.mark.parametrize("provider", [ReorderedFakePlanner(), DirectAwareFakePlanner()])
def test_two_policy_providers_are_replaceable_without_core_product_dependency(provider: PlanningPolicy) -> None:
    request = PlanningRequest("REQ-020", "同一输入", (task(), task("T2", depends_on=("T1",))))
    plan, decision = provider.plan(request)
    plan.validate()
    decision.validate()
    assert {node.task_id for node in plan.nodes} == {"T1", "T2"}
    assert next(node.depends_on for node in plan.nodes if node.task_id == "T2") == ("T1",)
    assert decision.input_fingerprint == fingerprint(request.to_dict())
