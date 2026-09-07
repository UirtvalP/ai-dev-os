"""无外部副作用的默认规则策略；不解析自然语言、不硬编码模型、不越权推进状态。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from ..agent_runtime.contracts import ModelDescriptor, RuntimeDescriptor
from .contracts import (
    ExecutionPlan,
    ModelRoute,
    PlanningRequest,
    PolicyDecision,
    PolicyError,
    RecoveryAction,
    RecoveryContext,
    RecoveryDecision,
    TaskSpec,
    VerificationPlan,
    VerificationPlanningRequest,
    commands_fingerprint,
    fingerprint,
)


def _decision(provider_id: str, reason: str, inputs: dict[str, Any], output: dict[str, Any]) -> PolicyDecision:
    return PolicyDecision(provider_id, "1", reason, fingerprint(inputs), output)


class RulePlanningPolicy:
    """只选择显式任务列表的执行形态；专业 Planner 可通过端口替换。"""

    def plan(self, request: PlanningRequest) -> tuple[ExecutionPlan, PolicyDecision]:
        request.validate()
        inputs = request.to_dict()
        mode: Literal["direct", "dag"] = "direct" if len(request.tasks) == 1 else "dag"
        result = ExecutionPlan(
            "plan-" + fingerprint(inputs), request.requirement_id, mode, request.tasks,
        )
        decision = _decision(
            "local.structured-planning", "单个显式任务直接执行" if mode == "direct"
            else "多个显式任务按已声明依赖执行，未生成或改写子任务", inputs, result.to_dict(),
        )
        return result, decision


def _runtime_inputs(runtimes: tuple[RuntimeDescriptor, ...]) -> list[dict[str, Any]]:
    if not isinstance(runtimes, tuple) or any(not isinstance(item, RuntimeDescriptor) for item in runtimes):
        raise PolicyError("invalid_runtime", "Runtime 描述必须是 RuntimeDescriptor tuple")
    identifiers: set[str] = set()
    result: list[dict[str, Any]] = []
    for runtime in runtimes:
        if (type(runtime.schema_version) is not int or runtime.schema_version != 1
                or type(runtime.available) is not bool or not isinstance(runtime.runtime_id, str)
                or not runtime.runtime_id.strip()
                or runtime.runtime_id in identifiers):
            raise PolicyError("invalid_runtime", "Runtime 描述版本、身份或可用状态无效/重复")
        if (not isinstance(runtime.capabilities, tuple)
                or any(not isinstance(item, str) or not item for item in runtime.capabilities)
                or len(set(runtime.capabilities)) != len(runtime.capabilities)
                or not isinstance(runtime.models, tuple)
                or any(not isinstance(item, ModelDescriptor) for item in runtime.models)):
            raise PolicyError("invalid_runtime", "Runtime 能力或模型列表格式无效")
        identifiers.add(runtime.runtime_id)
        model_ids: set[str] = set()
        for model in runtime.models:
            if not isinstance(model.id, str) or not model.id.strip() or model.id in model_ids:
                raise PolicyError("invalid_runtime", "Runtime 模型 ID 为空或重复")
            model_ids.add(model.id)
            if (not isinstance(model.reasoning_efforts, tuple)
                    or any(not isinstance(effort, str) or not effort for effort in model.reasoning_efforts)
                    or len(set(model.reasoning_efforts)) != len(model.reasoning_efforts)
                    or type(model.is_default) is not bool):
                raise PolicyError("invalid_runtime", "Runtime reasoning effort 列表无效")
        result.append(asdict(runtime))
    fingerprint(result)
    return result


def validate_route(task: TaskSpec, route: ModelRoute, runtimes: tuple[RuntimeDescriptor, ...]) -> None:
    """Supervisor 应对任何可替换 Router 的输出再次调用此边界校验。"""

    task.validate()
    route.validate()
    _runtime_inputs(runtimes)
    runtime = next((item for item in runtimes if item.runtime_id == route.runtime_id), None)
    if runtime is None or not runtime.available:
        raise PolicyError("no_route", "所选 Runtime 未报告可用")
    expected_profile = "workspace-write" if task.write_required else "read-only"
    if route.sandbox != expected_profile:
        raise PolicyError("invalid_route", "路由不能扩大只读任务权限或降低写任务隔离")
    required = {*task.required_capabilities, "start", "events", f"profile:{expected_profile}"}
    if not required <= set(runtime.capabilities):
        raise PolicyError("no_route", "所选 Runtime 未报告任务必需的能力/profile")
    model = next((item for item in runtime.models if item.id == route.model), None)
    if model is None or (route.effort is not None and route.effort not in model.reasoning_efforts):
        raise PolicyError("no_route", "所选模型或 effort 未由 Runtime 实际报告")
    for preferred, actual in ((task.preferred_runtime, route.runtime_id),
                              (task.preferred_model, route.model), (task.preferred_effort, route.effort)):
        if preferred is not None and preferred != actual:
            raise PolicyError("no_route", "路由不得静默忽略显式偏好")


def _effort(task: TaskSpec, model: ModelDescriptor) -> str | None:
    if task.preferred_effort is not None:
        return task.preferred_effort
    # 未知 effort 没有可比较的语义等级，不猜测大小；保持 Provider 默认值。
    ordering = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    reported = [effort for effort in ordering if effort in model.reasoning_efforts]
    if not reported:
        return None
    index = 0 if task.complexity == "tiny" else len(reported) - 1 if task.complexity == "complex" else len(reported) // 2
    return reported[index]


class CapabilityModelRouter:
    """优先显式偏好/Provider 默认模型，以真实能力与 effort 列表进行确定性选择。"""

    def route(
        self, task: TaskSpec, runtimes: tuple[RuntimeDescriptor, ...]
    ) -> tuple[ModelRoute, PolicyDecision]:
        task.validate()
        descriptions = _runtime_inputs(runtimes)
        choices: list[tuple[RuntimeDescriptor, ModelDescriptor, ModelRoute]] = []
        for runtime in runtimes:
            for model in runtime.models:
                route = ModelRoute(runtime.runtime_id, model.id, _effort(task, model),
                                   "workspace-write" if task.write_required else "read-only")
                try:
                    validate_route(task, route, runtimes)
                except PolicyError as error:
                    if error.code != "no_route":
                        raise
                    continue
                choices.append((runtime, model, route))
        if not choices:
            raise PolicyError("no_route", "没有同时满足真实模型、effort、权限配置和显式偏好的 Runtime")
        choices.sort(key=lambda item: (not item[1].is_default, item[0].runtime_id, item[1].id))
        route = choices[0][2]
        reason = "根据实际 Runtime 能力/profile 筛选；尊重显式偏好，默认模型优先；effort 按任务复杂度选择"
        return route, _decision("local.capability-routing", reason,
                                {"task": task.to_dict(), "runtimes": descriptions}, route.to_dict())


class RuleVerificationPlanner:
    """冻结调用方明确提供的测试命令及候选，不能从 Markdown 推断已验证。"""

    def plan(self, request: VerificationPlanningRequest) -> tuple[VerificationPlan, PolicyDecision]:
        request.validate()
        inputs = request.to_dict()
        result = VerificationPlan(
            "verification-" + fingerprint(inputs), request.requirement_id, request.task_id,
            request.candidate_sha, request.candidate_tree, dict(request.environment), request.commands,
            commands_fingerprint(request.commands),
        )
        return result, _decision(
            "local.explicit-verification", "冻结显式测试命令、实际候选与环境；尚未执行且不宣称通过",
            inputs, result.to_dict(),
        )


class BoundedRecoveryPolicy:
    """只有可证明无重复副作用的可恢复故障才重试，预算耗尽必定收敛。"""

    def decide(self, context: RecoveryContext) -> tuple[RecoveryDecision, PolicyDecision]:
        context.validate()
        action: RecoveryAction
        if context.duplicate_risk or context.error_class in ("unknown", "ambiguous_result", "lease_lost"):
            action, reason = "escalate", "执行结果未知或有重复风险，必须先人工/受信 reconciliation"
        elif context.error_class in ("cancelled", "policy_violation", "invalid_contract", "isolation_failure"):
            action, reason = "stop", "取消、非法契约或隔离/权限错误不可自动重放"
        elif context.error_class in ("verification_fail", "verification_failed", "dependency_changed"):
            if context.replan_count < context.replan_budget:
                action, reason = "replan", "验证失败或依赖变化，需要新的受控计划而非原样重试"
            else:
                action, reason = "escalate", "重规划预算已耗尽"
        elif context.error_class in ("crash", "timeout", "provider_offline", "unavailable"):
            # attempts 表示已消耗的 retry 次数，不含最初的首次执行。
            if context.attempts < context.retry_budget:
                action, reason = "retry", "无重复风险且仍有重试预算"
            else:
                action, reason = "stop", "重试预算已耗尽"
        else:
            action, reason = "escalate", "未知错误类别不能推断为可安全重试"
        result = RecoveryDecision(action, reason)
        return result, _decision("local.bounded-recovery", reason, context.to_dict(), result.to_dict())
