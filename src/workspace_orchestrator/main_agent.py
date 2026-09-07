"""Requirement Owner/Main Agent 的 Provider 无关持久状态。"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .executions import Execution, ExecutionStore
from .orchestration.contracts import ExecutionRecommendation
from .orchestration.store import OrchestrationStore
from .workspace import WorkspaceError, WorkspaceStore, markdown_sections, now_iso

LoopStage = Literal["observe", "assess", "plan", "act", "inspect", "review", "replan"]
ActionKind = Literal[
    "CreateTask", "StartExecution", "SendMessage", "CancelExecution", "RetryExecution",
    "ChangeModel", "RequestReview", "RunVerification", "AskUser", "CompleteRequirement",
]
_NEXT_STAGE: dict[LoopStage, LoopStage] = {
    "observe": "assess", "assess": "plan", "plan": "act", "act": "inspect",
    "inspect": "review", "review": "replan", "replan": "observe",
}
_ACTION_KINDS = {
    "CreateTask", "StartExecution", "SendMessage", "CancelExecution", "RetryExecution",
    "ChangeModel", "RequestReview", "RunVerification", "AskUser", "CompleteRequirement",
}


@dataclass(frozen=True, slots=True)
class MainAgentAction:
    id: str
    kind: ActionKind
    reason: str
    payload: dict[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        if not self.id.strip() or self.kind not in _ACTION_KINDS or not self.reason.strip():
            raise ValueError("Main Agent Action 身份、类型或原因不合法")
        if not isinstance(self.payload, dict):
            raise TypeError("Main Agent Action payload 必须是对象")
        json.dumps(self.payload, ensure_ascii=False, allow_nan=False)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(asdict(self))


@dataclass(frozen=True, slots=True)
class AcceptanceStatus:
    status: Literal["pending", "in_progress", "passed", "failed"]
    completed: int
    total: int

    def __post_init__(self) -> None:
        if self.status not in {"pending", "in_progress", "passed", "failed"}:
            raise ValueError("acceptance status 不合法")
        if (
            type(self.completed) is not int or type(self.total) is not int
            or self.completed < 0 or self.total < self.completed
        ):
            raise ValueError("acceptance 计数不合法")


@dataclass(frozen=True, slots=True)
class RequirementOwnerState:
    requirement_id: str
    current_goal: str
    current_phase: str
    active_tasks: tuple[str, ...]
    active_executions: tuple[str, ...]
    completed_tasks: tuple[str, ...]
    blocked_tasks: tuple[str, ...]
    acceptance_status: AcceptanceStatus
    recent_decisions: tuple[str, ...]
    known_risks: tuple[str, ...]
    next_actions: tuple[str, ...]
    review_required: bool
    intent: dict[str, str] = field(default_factory=dict)
    acceptance_criteria: tuple[str, ...] = ()
    verification_summary: str = ""
    git_state: dict[str, Any] = field(default_factory=dict)
    supervisor_signals: tuple[str, ...] = ()
    loop_stage: LoopStage = "observe"
    cycle: int = 1
    revision: int = 1
    actions: tuple[MainAgentAction, ...] = ()
    updated_at: str = field(default_factory=now_iso)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1 or not self.requirement_id.strip()
            or not self.current_goal.strip() or not self.current_phase.strip()
            or self.loop_stage not in _NEXT_STAGE or self.cycle < 1 or self.revision < 1
            or type(self.review_required) is not bool
        ):
            raise ValueError("Main Agent State 核心字段不合法")
        for values in (
            self.active_tasks, self.active_executions, self.completed_tasks,
            self.blocked_tasks, self.recent_decisions, self.known_risks, self.next_actions,
            self.acceptance_criteria, self.supervisor_signals,
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise ValueError("Main Agent State 列表字段必须是非空字符串 tuple")
        if (
            not isinstance(self.intent, dict)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in self.intent.items())
            or not isinstance(self.git_state, dict)
            or not isinstance(self.verification_summary, str)
        ):
            raise ValueError("Main Agent State 结构化事实字段不合法")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RequirementOwnerState:
        if value.get("schema_version") != 1:
            raise WorkspaceError("不支持 Main Agent State schema_version")
        try:
            acceptance = AcceptanceStatus(**value["acceptance_status"])
            actions = tuple(MainAgentAction(**item) for item in value.get("actions", []))
            return cls(
                requirement_id=value["requirement_id"], current_goal=value["current_goal"],
                current_phase=value["current_phase"],
                active_tasks=tuple(value["active_tasks"]),
                active_executions=tuple(value["active_executions"]),
                completed_tasks=tuple(value["completed_tasks"]),
                blocked_tasks=tuple(value["blocked_tasks"]),
                acceptance_status=acceptance,
                recent_decisions=tuple(value["recent_decisions"]),
                known_risks=tuple(value["known_risks"]),
                next_actions=tuple(value["next_actions"]),
                review_required=value["review_required"],
                intent=dict(value.get("intent", {})),
                acceptance_criteria=tuple(value.get("acceptance_criteria", ())),
                verification_summary=value.get("verification_summary", ""),
                git_state=copy.deepcopy(value.get("git_state", {})),
                supervisor_signals=tuple(value.get("supervisor_signals", ())),
                loop_stage=value["loop_stage"],
                cycle=value["cycle"], revision=value["revision"], actions=actions,
                updated_at=value["updated_at"], schema_version=value["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(f"Main Agent State 损坏：{exc}") from exc


class RequirementOwner:
    """只读取稳定事实并持久化决策上下文；不依赖 conversation 或 Provider。"""

    def __init__(self, workspace: WorkspaceStore, requirement_id: str) -> None:
        self.workspace = workspace
        self.requirement_id = requirement_id.upper()
        self.executions = ExecutionStore(workspace)

    def recommend_execution(
        self, *, role: str, parallelism: int = 1, runtime_id: str | None = None,
        model: str | None = None, effort: str | None = None, reason: str = "",
    ) -> ExecutionRecommendation:
        """形成 Provider 无关软建议；是否可执行必须再交给 Routing Policy 裁决。"""

        self.workspace.load(self.requirement_id)
        return ExecutionRecommendation(
            f"main-agent:{self.requirement_id}", runtime_id, model, effort,
            role, parallelism, reason,
        )

    @property
    def path(self) -> Path:
        return self.workspace.path_for(self.requirement_id) / "main-agent.json"

    def observe(self, *, expected_revision: int) -> RequirementOwnerState:
        with self.workspace.locked(self.requirement_id):
            return self._observe_locked(expected_revision)

    def _observe_locked(self, expected_revision: int) -> RequirementOwnerState:
        data = self.workspace.load(self.requirement_id)
        requirement = markdown_sections(data["requirement"])
        intent = markdown_sections(data["intent"])
        state = markdown_sections(data["state"])
        verification = markdown_sections(data["verification"])
        previous = self.load_optional()
        actual_revision = previous.revision if previous else 0
        if expected_revision != actual_revision:
            raise WorkspaceError(
                f"Main Agent State revision 已变化：期望 {expected_revision}，实际 {actual_revision}"
            )
        executions = self.executions.list(self.requirement_id)
        latest = self._latest_by_task(executions)
        active_status = {"queued", "starting", "running", "waiting"}
        blocked_status = {"blocked", "failed"}
        acceptance_lines = _checkboxes(requirement.get("Acceptance Criteria", ""))
        completed_acceptance = sum(checked for checked, _text in acceptance_lines)
        meta_status = str(data["meta"].get("status", "in_progress"))
        acceptance_state: Literal["pending", "in_progress", "passed", "failed"] = (
            "passed" if meta_status == "done" else
            "failed" if meta_status == "failed" else
            "in_progress" if completed_acceptance else "pending"
        )
        decisions = markdown_sections(data["decisions"])
        result = RequirementOwnerState(
            requirement_id=self.requirement_id,
            current_goal=requirement.get("Goal", str(data["meta"].get("title", ""))).strip(),
            current_phase=state.get("Phase", "未记录").strip(),
            active_tasks=tuple(sorted(
                task_id for task_id, item in latest.items() if item.status in active_status
            )),
            active_executions=tuple(
                item.id for item in executions if item.status in active_status
            ),
            completed_tasks=tuple(sorted(
                task_id for task_id, item in latest.items() if item.status == "completed"
            )),
            blocked_tasks=tuple(sorted(
                task_id for task_id, item in latest.items() if item.status in blocked_status
            )),
            acceptance_status=AcceptanceStatus(
                acceptance_state, completed_acceptance, len(acceptance_lines),
            ),
            recent_decisions=tuple(
                f"{heading}: {body.strip()}" for heading, body in decisions.items()
            )[-10:],
            known_risks=_bullets(state.get("Blocked", "")),
            next_actions=_bullets(state.get("Next Action", "")),
            review_required=meta_status == "in_review",
            intent={heading: body.strip() for heading, body in intent.items()},
            acceptance_criteria=tuple(text for _checked, text in acceptance_lines),
            verification_summary=verification.get("Latest Check", "").strip(),
            git_state=copy.deepcopy(data["meta"].get("git", {})),
            supervisor_signals=self._supervisor_signals(),
            loop_stage=(
                "observe" if previous is None or previous.loop_stage == "replan"
                else previous.loop_stage
            ),
            cycle=previous.cycle + 1 if previous and previous.loop_stage == "replan" else (
                previous.cycle if previous else 1
            ),
            revision=previous.revision + 1 if previous else 1,
            actions=previous.actions if previous else (),
        )
        self.workspace.write_json(self.path, result.to_dict())
        return result

    def advance(
        self, expected_stage: LoopStage, *, expected_revision: int,
    ) -> RequirementOwnerState:
        with self.workspace.locked(self.requirement_id):
            current = self.load()
            if current.revision != expected_revision:
                raise WorkspaceError(
                    f"Main Agent State revision 已变化：期望 {expected_revision}，实际 {current.revision}"
                )
            if current.loop_stage != expected_stage:
                raise WorkspaceError(
                    f"Main Agent loop stage 已变化：期望 {expected_stage}，实际 {current.loop_stage}"
                )
            updated = self._replace(
                current, loop_stage=_NEXT_STAGE[expected_stage], revision=current.revision + 1,
            )
            self.workspace.write_json(self.path, updated.to_dict())
            return updated

    def record_action(
        self, kind: ActionKind, reason: str, payload: dict[str, Any], *, expected_revision: int,
    ) -> RequirementOwnerState:
        with self.workspace.locked(self.requirement_id):
            current = self.load()
            if current.revision != expected_revision:
                raise WorkspaceError(
                    f"Main Agent State revision 已变化：期望 {expected_revision}，实际 {current.revision}"
                )
            if current.loop_stage != "act":
                raise WorkspaceError("只有 Main Agent act 阶段可以记录结构化 Action")
            if not reason.strip() or not isinstance(payload, dict):
                raise WorkspaceError("Main Agent Action 必须包含 reason 与对象 payload")
            try:
                action = MainAgentAction(
                    str(uuid4()), kind, reason.strip(), copy.deepcopy(payload), now_iso(),
                )
            except (TypeError, ValueError) as exc:
                raise WorkspaceError(f"Main Agent Action 不合法：{exc}") from exc
            updated = self._replace(
                current, actions=(*current.actions[-99:], action), loop_stage="inspect",
                revision=current.revision + 1,
            )
            self.workspace.write_json(self.path, updated.to_dict())
            return updated

    def load(self) -> RequirementOwnerState:
        if not self.path.is_file():
            raise WorkspaceError(f"Main Agent State 尚未创建：{self.requirement_id}")
        return RequirementOwnerState.from_dict(self.workspace.read_json(self.path))

    def load_optional(self) -> RequirementOwnerState | None:
        return self.load() if self.path.is_file() else None

    def current_revision(self) -> int:
        current = self.load_optional()
        return current.revision if current else 0

    def _supervisor_signals(self) -> tuple[str, ...]:
        root = self.workspace.path_for(self.requirement_id) / "orchestration" / "supervisor"
        if not (root / "state.json").is_file():
            return ()
        snapshot = OrchestrationStore(root).snapshot()
        result = [f"revision={snapshot['revision']}", f"fence={snapshot['fence']}"]
        lease = snapshot.get("lease")
        if isinstance(lease, dict):
            result.append(f"lease={lease.get('owner')}:{lease.get('fence')}")
        nodes = snapshot.get("data", {}).get("nodes", {})
        if isinstance(nodes, dict):
            for task_id, node in sorted(nodes.items()):
                if not isinstance(node, dict):
                    continue
                state = node.get("state", node.get("status", "unknown"))
                result.append(f"node={task_id}:{state}")
        return tuple(result)

    @staticmethod
    def _latest_by_task(executions: tuple[Execution, ...]) -> dict[str, Execution]:
        result: dict[str, Execution] = {}
        for execution in executions:
            result[execution.task_id] = execution
        return result

    @staticmethod
    def _replace(state: RequirementOwnerState, **changes: Any) -> RequirementOwnerState:
        return replace(state, **changes, updated_at=now_iso())


def _bullets(value: str) -> tuple[str, ...]:
    result = tuple(
        line[2:].strip() for line in value.splitlines()
        if line.startswith("- ") and line[2:].strip() not in {"无", "none", "None"}
    )
    if result:
        return result
    stripped = value.strip()
    return () if not stripped or stripped in {"无", "none", "None"} else (stripped,)


def _checkboxes(value: str) -> tuple[tuple[bool, str], ...]:
    result: list[tuple[bool, str]] = []
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [") and len(stripped) > 6 and stripped[4] == "]":
            result.append((stripped[3].lower() == "x", stripped[5:].strip()))
    return tuple(result)
