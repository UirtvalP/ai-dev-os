"""独立于 Main Agent 的确定性 Supervisor/Watchdog 信号层。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, cast

from .adapters.git import GitError, LocalGitProvider
from .agent_runtime.contracts import AgentEvent
from .agent_runtime.events import RuntimeEventStore
from .executions import Execution, ExecutionStore
from .orchestration.store import OrchestrationStore
from .workspace import WorkspaceError, WorkspaceStore, now_iso

SignalKind = Literal[
    "ExecutionStuck", "ExecutionLooping", "RepeatedFailure",
    "NoRequirementProgress", "BudgetWarning", "DuplicateWork",
    "MainAgentReviewRequired",
]
_SIGNAL_KINDS = {
    "ExecutionStuck", "ExecutionLooping", "RepeatedFailure",
    "NoRequirementProgress", "BudgetWarning", "DuplicateWork",
    "MainAgentReviewRequired",
}


@dataclass(frozen=True, slots=True)
class SupervisorSignal:
    id: str
    kind: SignalKind
    reason: str
    detected_at: str
    requirement_id: str
    execution_id: str | None = None
    task_id: str | None = None
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WatchdogPolicy:
    stuck_seconds: float = 1800
    loop_threshold: int = 3
    repeated_failure_threshold: int = 3
    stagnation_seconds: float = 3600
    stagnation_execution_count: int = 2
    token_budget: int = 200_000

    def __post_init__(self) -> None:
        numeric = (
            self.stuck_seconds, self.loop_threshold, self.repeated_failure_threshold,
            self.stagnation_seconds, self.stagnation_execution_count, self.token_budget,
        )
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in numeric):
            raise ValueError("Watchdog 阈值必须是有限正数")


class RequirementWatchdog:
    """只观察并持久化 review_required 信号；不取消或启动 Execution。"""

    def __init__(
        self, workspace: WorkspaceStore, requirement_id: str, *,
        policy: WatchdogPolicy | None = None, clock: Any = time.time,
    ) -> None:
        self.workspace = workspace
        self.requirement_id = requirement_id.upper()
        self.executions = ExecutionStore(workspace)
        self.events = RuntimeEventStore(workspace.root / "runtime-events")
        self.policy = policy or WatchdogPolicy()
        self.clock = clock

    @property
    def path(self) -> Path:
        return self.workspace.path_for(self.requirement_id) / "supervisor-watchdog.json"

    def scan(self) -> dict[str, Any]:
        with self.workspace.watchdog_locked(self.requirement_id):
            snapshot = self.workspace.load(self.requirement_id)
            executions = self.executions.list(self.requirement_id)
            previous = self.load()
            orchestration = OrchestrationStore(
                self.workspace.path_for(self.requirement_id) / "orchestration" / "supervisor",
            ).snapshot()
            now = float(self.clock())
            if not math.isfinite(now) or now < 0:
                raise WorkspaceError("Watchdog clock 必须是有限非负时间")
            acceptance = _acceptance_completed(snapshot["requirement"])
            owner_path = self.workspace.path_for(self.requirement_id) / "main-agent.json"
            owner = self.workspace.read_json(owner_path) if owner_path.is_file() else {}
            progress = {
                "acceptance_completed": acceptance,
                "executions": {item.id: item.status for item in executions},
                "task_transitions": _task_transitions(orchestration),
                "git_state": _git_state(self.workspace),
                "test_summary": snapshot["verification"],
                "main_agent_action_count": len(owner.get("actions", [])),
            }
            progress_fingerprint = hashlib.sha256(_canonical(progress).encode()).hexdigest()
            changed_at = now
            if previous and previous["observation"].get("progress_fingerprint") == progress_fingerprint:
                changed_at = float(previous["observation"]["progress_changed_at"])
            signals = self._signals(
                executions, orchestration, now, acceptance, changed_at,
            )
            if signals:
                signals.append(self._signal(
                    "MainAgentReviewRequired", "Supervisor 检测到需要 Main Agent Review 的异常",
                    now, evidence={"signal_ids": [item.id for item in signals]},
                ))
            state = {
                "schema_version": 1,
                "requirement_id": self.requirement_id,
                "revision": int(previous.get("revision", 0)) + 1 if previous else 1,
                "review_required": bool(signals),
                "signals": [item.to_dict() for item in signals],
                "observation": {
                    "at": now, "acceptance_completed": acceptance,
                    "progress_fingerprint": progress_fingerprint,
                    "progress_changed_at": changed_at,
                    "execution_count": len(executions),
                    "active_execution_count": sum(_active(item) for item in executions),
                    **progress,
                },
                "updated_at": now_iso(),
            }
            self.workspace.write_json(self.path, state)
            return state

    def load(self) -> dict[str, Any] | None:
        return load_watchdog_state(self.workspace, self.requirement_id)

    def _signals(
        self, executions: tuple[Execution, ...], orchestration: dict[str, Any], now: float,
        acceptance: int, acceptance_changed_at: float,
    ) -> list[SupervisorSignal]:
        result: list[SupervisorSignal] = []
        active_by_task: dict[str, list[str]] = {}
        failure_signatures: list[str | None] = []
        for execution in executions:
            events = self.events.replay(execution.id)
            if any(
                event.requirement_id != execution.requirement_id
                or event.task_id != execution.task_id
                or event.execution_id != execution.id
                or event.runtime_id != execution.runtime_id
                or event.session_id != execution.session_id
                for event in events
            ):
                raise WorkspaceError(f"Supervisor Event 身份与 Execution 不一致：{execution.id}")
            if _active(execution) and _task_allows_active(orchestration, execution.task_id):
                active_by_task.setdefault(execution.task_id, []).append(execution.id)
                last_progress = max(
                    (_timestamp(execution.last_progress_at or execution.started_at or execution.created_at),
                     *(_timestamp(event.timestamp) for event in events if _progress(event))),
                )
                if now - last_progress >= self.policy.stuck_seconds:
                    result.append(self._signal(
                        "ExecutionStuck", "运行中的 Execution 长时间没有事件、Git 或测试进展",
                        now, execution, {"idle_seconds": now - last_progress},
                    ))
            commands = [_command(event) for event in events]
            commands = [item for item in commands if item]
            repeated_command = _consecutive(commands)
            errors = [_error_signature(event) for event in events if event.kind == "error"]
            errors = [item for item in errors if item]
            repeated_error = _consecutive(errors)
            if max(repeated_command, repeated_error) >= self.policy.loop_threshold:
                result.append(self._signal(
                    "ExecutionLooping", "Execution 连续重复相同命令或错误",
                    now, execution,
                    {"command_repeat": repeated_command, "error_repeat": repeated_error},
                ))
            if execution.status == "failed" and execution.error:
                failure_signatures.append(_canonical(execution.error))
            else:
                failure_signatures.append(None)
            tokens = max((_tokens(event.payload) for event in events), default=0)
            if tokens > self.policy.token_budget:
                result.append(self._signal(
                    "BudgetWarning", "Execution 事件记录的 token 用量超过预算",
                    now, execution, {"tokens": tokens, "budget": self.policy.token_budget},
                ))
        for task_id, ids in active_by_task.items():
            if len(ids) > 1:
                result.append(self._signal(
                    "DuplicateWork", "同一 Task 存在多个活动 Execution",
                    now, task_id=task_id, evidence={"execution_ids": ids},
                ))
        failure_repeat = _consecutive_optional(failure_signatures)
        if failure_repeat >= self.policy.repeated_failure_threshold:
            result.append(self._signal(
                "RepeatedFailure", "多个 Execution 连续出现相同失败签名", now,
                evidence={"repeat": failure_repeat},
            ))
        if (
            len(executions) >= self.policy.stagnation_execution_count
            and now - acceptance_changed_at >= self.policy.stagnation_seconds
        ):
            result.append(self._signal(
                "NoRequirementProgress", "多个 Execution 后 Acceptance 长时间没有变化",
                now, evidence={
                    "acceptance_completed": acceptance,
                    "stagnation_seconds": now - acceptance_changed_at,
                },
            ))
        return result

    def _signal(
        self, kind: SignalKind, reason: str, now: float,
        execution: Execution | None = None, evidence: dict[str, Any] | None = None,
        *, task_id: str | None = None,
    ) -> SupervisorSignal:
        execution_id = execution.id if execution else None
        task = execution.task_id if execution else task_id
        return SupervisorSignal(
            _signal_id(kind, self.requirement_id, execution_id, task),
            kind, reason,
            datetime.fromtimestamp(now, UTC).astimezone().isoformat(timespec="seconds"),
            self.requirement_id, execution_id, task, evidence,
        )


def load_watchdog_state(
    workspace: WorkspaceStore, requirement_id: str,
) -> dict[str, Any] | None:
    """严格恢复 Watchdog 状态，供 Watchdog、Main Agent 与 Dashboard 共用。"""

    requirement_id = requirement_id.upper()
    path = workspace.path_for(requirement_id) / "supervisor-watchdog.json"
    if not path.is_file():
        return None
    value = workspace.read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("requirement_id") != requirement_id
        or type(value.get("revision")) is not int or value["revision"] < 1
        or type(value.get("review_required")) is not bool
        or not isinstance(value.get("signals"), list)
        or not isinstance(value.get("observation"), dict)
    ):
        raise WorkspaceError("Supervisor Watchdog 状态身份、版本或结构不合法")
    executions = ExecutionStore(workspace)
    for signal in value["signals"]:
        if (
            not isinstance(signal, dict)
            or signal.get("kind") not in _SIGNAL_KINDS
            or signal.get("requirement_id") != requirement_id
            or not isinstance(signal.get("reason"), str) or not signal["reason"].strip()
            or not isinstance(signal.get("detected_at"), str)
            or signal.get("evidence") is not None and not isinstance(signal["evidence"], dict)
        ):
            raise WorkspaceError("Supervisor Watchdog Signal 结构或 Requirement 身份不合法")
        execution_id, task_id = signal.get("execution_id"), signal.get("task_id")
        if execution_id is not None:
            if not isinstance(execution_id, str):
                raise WorkspaceError("Supervisor Signal execution_id 不合法")
            execution = executions.get(execution_id)
            if execution.requirement_id != requirement_id or execution.task_id != task_id:
                raise WorkspaceError("Supervisor Signal Execution/Task 身份不匹配")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise WorkspaceError("Supervisor Signal task_id 不合法")
        expected_id = _signal_id(signal["kind"], requirement_id, execution_id, task_id)
        if signal.get("id") != expected_id:
            raise WorkspaceError("Supervisor Signal ID 与身份不匹配")
        try:
            datetime.fromisoformat(signal["detected_at"])
        except ValueError as exc:
            raise WorkspaceError("Supervisor Signal detected_at 不合法") from exc
    kinds = [signal["kind"] for signal in value["signals"]]
    if value["review_required"] != bool(kinds) or (
        bool(kinds) and kinds.count("MainAgentReviewRequired") != 1
    ):
        raise WorkspaceError("Supervisor review_required 与 Signal 集合不一致")
    observation = value["observation"]
    if "progress_fingerprint" not in observation:
        # P8 首个 Demo 曾写入只跟踪 Acceptance 的草案；仅无信号状态可安全升级。
        legacy_changed = observation.get("acceptance_changed_at")
        if kinds or type(legacy_changed) not in (int, float):
            raise WorkspaceError("Supervisor Watchdog legacy observation 不能安全升级")
        observation["progress_fingerprint"] = hashlib.sha256(
            _canonical(observation).encode()
        ).hexdigest()
        observation["progress_changed_at"] = legacy_changed
    if (
        not isinstance(observation.get("progress_fingerprint"), str)
        or len(observation["progress_fingerprint"]) != 64
        or type(observation.get("progress_changed_at")) not in (int, float)
    ):
        raise WorkspaceError("Supervisor Watchdog observation 不合法")
    return cast(dict[str, Any], value)


def _signal_id(
    kind: str, requirement_id: str, execution_id: str | None, task_id: str | None,
) -> str:
    identity = _canonical({
        "kind": kind, "requirement_id": requirement_id,
        "execution_id": execution_id, "task_id": task_id,
    })
    return "SIG-" + hashlib.sha256(identity.encode()).hexdigest()[:12]


def _task_transitions(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = snapshot.get("data", {}).get("nodes", {})
    if not isinstance(nodes, dict):
        return {}
    return {
        task_id: {
            "status": str(node.get("status", node.get("state", "unknown"))),
            "revision": node.get("revision"),
            "active_attempt_id": node.get("active_attempt_id"),
        }
        for task_id, node in nodes.items() if isinstance(task_id, str) and isinstance(node, dict)
    }


def _task_allows_active(snapshot: dict[str, Any], task_id: str) -> bool:
    data = snapshot.get("data", {})
    nodes = data.get("nodes", {}) if isinstance(data, dict) else {}
    if not nodes:
        return True
    node = nodes.get(task_id) if isinstance(nodes, dict) else None
    return bool(
        isinstance(node, dict)
        and node.get("active_attempt_id")
        and node.get("status") in {"dispatching", "running", "unknown"}
    )


def _git_state(workspace: WorkspaceStore) -> dict[str, Any]:
    try:
        return dict(LocalGitProvider(workspace.working_root).status())
    except GitError as exc:
        return {"error": str(exc)}


def _active(execution: Execution) -> bool:
    return execution.status in {"starting", "running"}


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _progress(event: AgentEvent) -> bool:
    return event.kind in {"message", "tool", "completion"} or any(
        word in _canonical(event.payload).lower() for word in ("git", "test", "file")
    )


def _command(event: AgentEvent) -> str:
    values = _values(event.payload, {"command", "cmd"})
    return values[-1] if values else ""


def _error_signature(event: AgentEvent) -> str:
    return hashlib.sha256(_canonical(event.payload).encode()).hexdigest()


def _values(value: Any, keys: set[str]) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in keys and isinstance(item, str):
                result.append(item.strip())
            result.extend(_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            result.extend(_values(item, keys))
    return result


def _tokens(value: Any) -> int:
    total = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"total_tokens", "totaltokens"} and type(item) is int:
                total += max(item, 0)
            else:
                total += _tokens(item)
    elif isinstance(value, list):
        total += sum(_tokens(item) for item in value)
    return total


def _consecutive(values: list[str]) -> int:
    if not values:
        return 0
    current = maximum = 1
    for previous, value in pairwise(values):
        current = current + 1 if value == previous else 1
        maximum = max(maximum, current)
    return maximum


def _consecutive_optional(values: list[str | None]) -> int:
    current = maximum = 0
    previous: str | None = None
    for value in values:
        current = current + 1 if value is not None and value == previous else int(value is not None)
        maximum = max(maximum, current)
        previous = value
    return maximum


def _acceptance_completed(requirement: str) -> int:
    return len(re.findall(r"(?m)^\s*- \[[xX]\] ", requirement))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
