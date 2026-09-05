"""Phase 2 的最小版本化数据契约；数据合法不等同来源可信或获得状态推进权限。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Literal, Self

SCHEMA_VERSION = 1
Complexity = Literal["tiny", "normal", "complex"]
RecoveryAction = Literal["retry", "replan", "escalate", "stop"]
WorkerState = Literal["running", "candidate_complete", "blocked", "failed", "unknown"]


class PolicyError(ValueError):
    """策略输入、输出或可用能力不满足约束；调用方必须失败关闭。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def fingerprint(value: Any) -> str:
    """只对 JSON 数据计算稳定摘要，不依赖 Python repr 或对象地址。"""

    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PolicyError("invalid_contract", "指纹输入必须是有效的有限 JSON 数据") from exc
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise PolicyError("invalid_contract", f"{name} 必须是非空、无控制字符字符串")


def _integer(value: Any, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise PolicyError("invalid_contract", f"{name} 必须是大于等于 {minimum} 的整数")


def _strings(value: Any, name: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        raise PolicyError("invalid_contract", f"{name} 必须是字符串 tuple")
    for item in value:
        _text(item, name)
    if len(set(value)) != len(value):
        raise PolicyError("invalid_contract", f"{name} 不得重复")


def _sha(value: Any, name: str, *, digest: bool = False) -> None:
    size = r"[a-f0-9]{64}" if digest else r"(?:[a-f0-9]{40}|[a-f0-9]{64})"
    if not isinstance(value, str) or re.fullmatch(size, value) is None:
        raise PolicyError("invalid_contract", f"{name} 必须是完整的小写十六进制摘要")


def _environment(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise PolicyError("invalid_contract", "验证 environment 必须是非空字符串对象")
    for key, item in value.items():
        _text(key, "environment key")
        _text(item, "environment value")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Contract:
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise PolicyError("unsupported_schema", "不支持的 Orchestration schema_version")
        if not isinstance(self.extra, dict):
            raise PolicyError("invalid_contract", "扩展字段必须是对象")
        fingerprint(self.extra)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = _json_value(self.extra)
        result.update({
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self) if item.name != "extra"
        })
        return result

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        return values

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        if not isinstance(payload, Mapping):
            raise PolicyError("invalid_contract", "契约必须是 JSON 对象")
        names = {item.name for item in fields(cls)} - {"extra"}
        values = {name: copy.deepcopy(value) for name, value in payload.items() if name in names}
        if "schema_version" not in values:
            raise PolicyError("unsupported_schema", "持久契约缺少 schema_version")
        values["extra"] = copy.deepcopy({name: value for name, value in payload.items() if name not in names})
        try:
            return cls(**cls._decode(values))
        except (TypeError, KeyError) as exc:
            raise PolicyError("invalid_contract", f"{cls.__name__} 字段不完整或类型错误：{exc}") from exc


def _json_value(value: Any) -> Any:
    """在持久边界转换 tuple；嵌套契约也独立展开未知字段，避免 asdict 丢失扩展。"""
    if isinstance(value, _Contract):
        return value.to_dict()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise PolicyError("invalid_contract", "JSON 对象的字段名必须是字符串")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise PolicyError("invalid_contract", "持久字段必须是有限 JSON 数据")


@dataclass(frozen=True, slots=True)
class TaskSpec(_Contract):
    task_id: str
    title: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    complexity: Complexity = "normal"
    write_required: bool = False
    required_capabilities: tuple[str, ...] = ("start", "message", "events")
    worktree: str | None = None
    branch: str | None = None
    retry_budget: int = 1
    preferred_runtime: str | None = None
    preferred_model: str | None = None
    preferred_effort: str | None = None

    def validate(self) -> None:
        _Contract.validate(self)
        for name in ("task_id", "title"):
            _text(getattr(self, name), name)
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise PolicyError("invalid_contract", "Task prompt 不能为空")
        _strings(self.depends_on, "depends_on")
        _strings(self.required_capabilities, "required_capabilities")
        if self.task_id in self.depends_on:
            raise PolicyError("invalid_plan", "任务不得依赖自己")
        if self.complexity not in ("tiny", "normal", "complex") or type(self.write_required) is not bool:
            raise PolicyError("invalid_contract", "Task complexity/write_required 不合法")
        _integer(self.retry_budget, "retry_budget")
        for name in ("worktree", "branch", "preferred_runtime", "preferred_model", "preferred_effort"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        for name in ("depends_on", "required_capabilities"):
            if isinstance(values.get(name), list):
                values[name] = tuple(values[name])
        return values


@dataclass(frozen=True, slots=True)
class PlanningRequest(_Contract):
    requirement_id: str
    goal: str
    tasks: tuple[TaskSpec, ...]

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.requirement_id, "requirement_id")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise PolicyError("invalid_contract", "目标不能为空")
        _validate_graph(self.tasks)

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        values["tasks"] = tuple(TaskSpec.from_dict(item) for item in values["tasks"])
        return values

    def to_dict(self) -> dict[str, Any]:
        result = _Contract.to_dict(self)
        result["tasks"] = [task.to_dict() for task in self.tasks]
        return result


def _validate_graph(nodes: tuple[TaskSpec, ...]) -> None:
    if not isinstance(nodes, tuple) or not nodes or any(not isinstance(item, TaskSpec) for item in nodes):
        raise PolicyError("invalid_plan", "ExecutionPlan 必须包含非空 TaskSpec tuple")
    for node in nodes:
        node.validate()
    tasks = {node.task_id: node for node in nodes}
    if len(tasks) != len(nodes):
        raise PolicyError("invalid_plan", "ExecutionPlan 包含重复 Task ID")
    for node in nodes:
        if set(node.depends_on) - tasks.keys():
            raise PolicyError("invalid_plan", f"{node.task_id} 包含未知依赖")
    remaining = {node.task_id: set(node.depends_on) for node in nodes}
    while remaining:
        ready = {task_id for task_id, deps in remaining.items() if not deps}
        if not ready:
            raise PolicyError("invalid_plan", "ExecutionPlan 包含依赖环")
        remaining = {task_id: deps - ready for task_id, deps in remaining.items() if task_id not in ready}


@dataclass(frozen=True, slots=True)
class ExecutionPlan(_Contract):
    plan_id: str
    requirement_id: str
    mode: Literal["direct", "dag"]
    nodes: tuple[TaskSpec, ...]

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.plan_id, "plan_id")
        _text(self.requirement_id, "requirement_id")
        _validate_graph(self.nodes)
        if self.mode not in ("direct", "dag") or (self.mode == "direct" and len(self.nodes) != 1):
            raise PolicyError("invalid_plan", "direct 只能含单任务；mode 必须是 direct 或 dag")

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        values["nodes"] = tuple(TaskSpec.from_dict(item) for item in values["nodes"])
        return values

    def to_dict(self) -> dict[str, Any]:
        result = _Contract.to_dict(self)
        result["nodes"] = [node.to_dict() for node in self.nodes]
        return result


@dataclass(frozen=True, slots=True)
class PolicyDecision(_Contract):
    provider_id: str
    provider_version: str
    reason: str
    input_fingerprint: str
    decision: dict[str, Any]

    def validate(self) -> None:
        _Contract.validate(self)
        for name in ("provider_id", "provider_version", "reason"):
            _text(getattr(self, name), name)
        _sha(self.input_fingerprint, "input_fingerprint", digest=True)
        if not isinstance(self.decision, dict):
            raise PolicyError("invalid_contract", "decision 必须是 JSON 对象")
        fingerprint(self.decision)


@dataclass(frozen=True, slots=True)
class ModelRoute(_Contract):
    runtime_id: str
    model: str
    effort: str | None
    sandbox: str

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.runtime_id, "runtime_id")
        _text(self.model, "model")
        if self.effort is not None:
            _text(self.effort, "effort")
        if self.sandbox not in ("read-only", "workspace-write"):
            raise PolicyError("invalid_route", "不允许未隔离或未知 sandbox")


@dataclass(frozen=True, slots=True)
class VerificationCommand(_Contract):
    command_id: str
    argv: tuple[str, ...]
    timeout_seconds: int = 300

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.command_id, "command_id")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise PolicyError("invalid_contract", "验证 argv 不能为空，且不得使用 shell 字符串")
        for argument in self.argv:
            if not isinstance(argument, str) or "\0" in argument:
                raise PolicyError("invalid_contract", "argv 必须是无 NUL 字符串")
        _text(self.argv[0], "可执行程序")
        _integer(self.timeout_seconds, "timeout_seconds", 1)

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        if isinstance(values.get("argv"), list):
            values["argv"] = tuple(values["argv"])
        return values


def commands_fingerprint(commands: tuple[VerificationCommand, ...]) -> str:
    if not isinstance(commands, tuple) or not commands:
        raise PolicyError("invalid_verification", "验证命令列表不能为空")
    if any(not isinstance(command, VerificationCommand) for command in commands):
        raise PolicyError("invalid_verification", "验证命令必须遵循 VerificationCommand")
    identifiers = [command.command_id for command in commands]
    if len(set(identifiers)) != len(identifiers):
        raise PolicyError("invalid_verification", "验证 command_id 不得重复")
    return fingerprint([command.to_dict() for command in commands])


@dataclass(frozen=True, slots=True)
class VerificationPlanningRequest(_Contract):
    requirement_id: str
    task_id: str
    candidate_sha: str
    candidate_tree: str
    environment: dict[str, str]
    commands: tuple[VerificationCommand, ...]

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.requirement_id, "requirement_id")
        _text(self.task_id, "task_id")
        _sha(self.candidate_sha, "candidate_sha")
        _sha(self.candidate_tree, "candidate_tree")
        _environment(self.environment)
        commands_fingerprint(self.commands)

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        values["commands"] = tuple(VerificationCommand.from_dict(item) for item in values["commands"])
        return values

    def to_dict(self) -> dict[str, Any]:
        result = _Contract.to_dict(self)
        result["commands"] = [command.to_dict() for command in self.commands]
        return result


@dataclass(frozen=True, slots=True)
class VerificationPlan(_Contract):
    plan_id: str
    requirement_id: str
    task_id: str
    candidate_sha: str
    candidate_tree: str
    environment: dict[str, str]
    commands: tuple[VerificationCommand, ...]
    commands_fingerprint: str

    def validate(self) -> None:
        _Contract.validate(self)
        VerificationPlanningRequest(
            self.requirement_id, self.task_id, self.candidate_sha, self.candidate_tree,
            self.environment, self.commands,
        ).validate()
        _text(self.plan_id, "plan_id")
        if self.commands_fingerprint != commands_fingerprint(self.commands):
            raise PolicyError("invalid_verification", "验证命令指纹与实际命令不一致")

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        values["commands"] = tuple(VerificationCommand.from_dict(item) for item in values["commands"])
        return values

    def to_dict(self) -> dict[str, Any]:
        result = _Contract.to_dict(self)
        result["commands"] = [command.to_dict() for command in self.commands]
        return result


@dataclass(frozen=True, slots=True)
class VerificationCommandResult(_Contract):
    command_id: str
    returncode: int
    stdout_sha256: str
    stderr_sha256: str
    duration_seconds: float = 0

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.command_id, "command_id")
        if type(self.returncode) is not int:
            raise PolicyError("invalid_verification", "returncode 必须是整数")
        _sha(self.stdout_sha256, "stdout_sha256", digest=True)
        _sha(self.stderr_sha256, "stderr_sha256", digest=True)
        if (isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, (int, float))
                or not math.isfinite(self.duration_seconds) or self.duration_seconds < 0):
            raise PolicyError("invalid_verification", "命令持续时间必须是非负有限数")


@dataclass(frozen=True, slots=True)
class VerificationReceiptEnvelope(_Contract):
    receipt_id: str
    plan_id: str
    requirement_id: str
    task_id: str
    candidate_sha: str
    candidate_tree: str
    environment: dict[str, str]
    commands_fingerprint: str
    results: tuple[VerificationCommandResult, ...]
    started_at: str
    completed_at: str
    provider_id: str
    provider_version: str

    def validate(self) -> None:
        _Contract.validate(self)
        for name in ("receipt_id", "plan_id", "requirement_id", "task_id", "provider_id", "provider_version"):
            _text(getattr(self, name), name)
        _sha(self.candidate_sha, "candidate_sha")
        _sha(self.candidate_tree, "candidate_tree")
        _sha(self.commands_fingerprint, "commands_fingerprint", digest=True)
        _environment(self.environment)
        if (not isinstance(self.results, tuple) or not self.results
                or any(not isinstance(item, VerificationCommandResult) for item in self.results)):
            raise PolicyError("invalid_verification", "验证结果必须是非空结果 tuple")
        for item in self.results:
            item.validate()
        identifiers = [item.command_id for item in self.results]
        if len(set(identifiers)) != len(identifiers):
            raise PolicyError("invalid_verification", "验证结果 command_id 重复")
        try:
            start, end = datetime.fromisoformat(self.started_at), datetime.fromisoformat(self.completed_at)
            if start.tzinfo is None or end.tzinfo is None or end < start:
                raise ValueError("时间无时区或倒序")
        except (TypeError, ValueError) as exc:
            raise PolicyError("invalid_verification", "验证时间必须带时区且有序") from exc

    def validate_for(self, plan: VerificationPlan) -> None:
        """检查 PASS 证据绑定；来源身份与当前 Git 新鲜性仍由受控执行端验证。"""

        self.validate()
        plan.validate()
        for name in ("plan_id", "requirement_id", "task_id", "candidate_sha", "candidate_tree",
                     "environment", "commands_fingerprint"):
            if getattr(self, name) != getattr(plan, name):
                raise PolicyError("stale_verification", f"验证回执 {name} 与计划不匹配")
        if tuple(item.command_id for item in self.results) != tuple(item.command_id for item in plan.commands):
            raise PolicyError("invalid_verification", "验证结果必须完整覆盖同一命令顺序")
        if any(item.returncode != 0 for item in self.results):
            raise PolicyError("verification_failed", "验证命令未全部成功")

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        values["results"] = tuple(VerificationCommandResult.from_dict(item) for item in values["results"])
        return values

    def to_dict(self) -> dict[str, Any]:
        result = _Contract.to_dict(self)
        result["results"] = [item.to_dict() for item in self.results]
        return result


@dataclass(frozen=True, slots=True)
class RecoveryContext(_Contract):
    error_class: str
    attempts: int
    retry_budget: int
    duplicate_risk: bool = False
    replan_count: int = 0
    replan_budget: int = 1

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.error_class, "error_class")
        for name in ("attempts", "retry_budget", "replan_count", "replan_budget"):
            _integer(getattr(self, name), name)
        if type(self.duplicate_risk) is not bool:
            raise PolicyError("invalid_recovery", "duplicate_risk 必须是布尔值")


@dataclass(frozen=True, slots=True)
class RecoveryDecision(_Contract):
    action: RecoveryAction
    reason: str

    def validate(self) -> None:
        _Contract.validate(self)
        if self.action not in ("retry", "replan", "escalate", "stop"):
            raise PolicyError("invalid_recovery", "未知恢复动作")
        _text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class WorkerObservation(_Contract):
    attempt_id: str
    fence: int
    state: WorkerState
    session_id: str | None = None
    candidate_sha: str | None = None
    candidate_tree: str | None = None
    error_class: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.attempt_id, "attempt_id")
        _integer(self.fence, "fence", 1)
        if self.state not in ("running", "candidate_complete", "blocked", "failed", "unknown"):
            raise PolicyError("invalid_worker_state", "Worker 无权报告 accepted/done")
        for name in ("session_id", "error_class"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        for name in ("candidate_sha", "candidate_tree"):
            if getattr(self, name) is not None:
                _sha(getattr(self, name), name)
        if self.state == "candidate_complete" and (not self.candidate_sha or not self.candidate_tree):
            raise PolicyError("invalid_worker_state", "候选完成必须绑定实际 commit 与 tree")
        if not isinstance(self.details, dict):
            raise PolicyError("invalid_worker_state", "Worker details 必须是对象")
        if not isinstance(self.summary, str):
            raise PolicyError("invalid_worker_state", "Worker summary 必须是字符串")
        fingerprint(self.details)


@dataclass(frozen=True, slots=True)
class WorkerIsolation(_Contract):
    mechanism: str
    enforced: bool
    writable_roots: tuple[str, ...]
    protected_roots: tuple[str, ...]
    reason: str = ""

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.mechanism, "mechanism")
        if type(self.enforced) is not bool:
            raise PolicyError("invalid_isolation", "隔离状态必须是布尔值")
        _strings(self.writable_roots, "writable_roots")
        _strings(self.protected_roots, "protected_roots")
        if not isinstance(self.reason, str):
            raise PolicyError("invalid_isolation", "隔离说明必须是字符串")

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        for name in ("writable_roots", "protected_roots"):
            if isinstance(values.get(name), list):
                values[name] = tuple(values[name])
        return values
