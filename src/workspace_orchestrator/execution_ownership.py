"""V1/V2 共用的持久执行认领；面板状态、标签和 Agent 文本都不是执行所有权。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .adapters.base import TaskProvider
from .models import Task
from .orchestration.contracts import ExecutionPlan, PlanningRequest
from .orchestration.store import OrchestrationStore, OrchestrationStoreError
from .workspace import WorkspaceError, WorkspaceStore


class ExecutionOwnershipError(WorkspaceError):
    """所有权冲突或证据不可读时禁止新的执行，不更改外部卡片。"""


class ExecutionOwnership:
    """认领不随 Supervisor 退出、Provider 离线或重启消失；本阶段不自动释放。"""

    def __init__(self, workspace: WorkspaceStore) -> None:
        self.workspace = workspace
        self.store = OrchestrationStore(workspace.root / "execution-ownership")

    def _claims(self) -> dict[str, Any]:
        try:
            data = self.store.snapshot()["data"]
        except OrchestrationStoreError as exc:
            raise ExecutionOwnershipError(str(exc)) from exc
        claims = data.get("claims", {})
        if not isinstance(claims, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            or value.get("engine") != "v2"
            or not all(isinstance(value.get(field), str) and value[field]
                       for field in ("requirement_id", "node_id"))
            or not isinstance(value.get("aliases"), list) or key not in value["aliases"]
            or any(not isinstance(alias, str) or not alias for alias in value["aliases"])
            for key, value in claims.items()
        ):
            raise ExecutionOwnershipError("执行所有权账本损坏，禁止认领")
        for value in claims.values():
            if any(claims.get(alias) != value for alias in value["aliases"]):
                raise ExecutionOwnershipError("Task 所有权别名不一致")
        return dict(claims)

    def require_v1(self, task: Task) -> None:
        with self.workspace.locked():
            claims = self._claims()
            if any(alias in claims for alias in (task.id, task.raw_id) if alias):
                raise ExecutionOwnershipError("Task 已由 V2 持久认领，V1 不得启动")

    def require_v2(self, requirement_id: str, node_id: str, task: Task) -> None:
        with self.workspace.locked():
            claims = self._claims()
            for alias in (task.id, task.raw_id):
                if alias and (claims.get(alias, {}).get("requirement_id"),
                              claims.get(alias, {}).get("node_id")) != (requirement_id, node_id):
                    raise ExecutionOwnershipError("投影缺少先于执行建立的 V2 持久认领")

    def _require_legacy_idle(self, requirement_id: str, aliases: set[str]) -> None:
        path = self.workspace.root / "dispatcher.json"
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.resolve() != path or path.stat().st_nlink != 1:
                raise ExecutionOwnershipError("Dispatcher 账本不能使用重定向文件")
            legacy = self.workspace.read_json(path)
            if not isinstance(legacy, dict) or not isinstance(legacy.get("tasks"), dict):
                raise ExecutionOwnershipError("Dispatcher 账本损坏，不能证明 Task 空闲")
            for key, value in legacy["tasks"].items():
                if not isinstance(value, dict):
                    raise ExecutionOwnershipError("Dispatcher Task 记录损坏")
                if aliases.intersection({key, value.get("task_id"), value.get("raw_id")}) and (
                    value.get("result") in (None, "dispatching", "cancel_requested")
                ):
                    raise ExecutionOwnershipError("旧 Dispatcher 已认领或执行结果未知，V2 不得启动")
        sessions = self.workspace.load(requirement_id)["sessions"]
        if any(session.get("result") == "in_progress"
               and aliases.intersection(session.get("task_ids", ())) for session in sessions):
            raise ExecutionOwnershipError("Task 已有活动 Session，V2 不得重复认领")

    def claim_plan(
        self, request: PlanningRequest, plan: ExecutionPlan,
        provider_factory: Callable[[], TaskProvider | None],
    ) -> None:
        bindings = request.extra.get("task_provider_bindings", {})
        if not isinstance(bindings, dict) or any(
            not isinstance(node, str) or not node or not isinstance(task, str) or not task
            for node, task in bindings.items()
        ) or len(set(bindings.values())) != len(bindings):
            raise ExecutionOwnershipError("Task Provider 绑定必须是唯一的节点到卡片映射")
        if set(bindings) - {task.task_id for task in request.tasks}:
            raise ExecutionOwnershipError("外部卡片只能绑定操作员明确提供的原始 Task")
        if request.requirement_id != plan.requirement_id:
            raise ExecutionOwnershipError("计划与认领需求不一致")
        if not bindings:
            return
        # 已持久确认的认领不依赖在线 Provider，因此重启/离线不能解除防重。
        with self.workspace.locked():
            claims = self._claims()
            missing = {node: card for node, card in bindings.items() if card not in claims}
            for node, card in bindings.items():
                if card in claims and (claims[card]["requirement_id"], claims[card]["node_id"]) != (
                    request.requirement_id, node,
                ):
                    raise ExecutionOwnershipError("已有 Task 不得转移给其他 V2 节点")
        if not missing:
            return
        provider = provider_factory()
        if provider is None:
            raise ExecutionOwnershipError("首次认领需读取已配置的 Task Provider")
        prepared: dict[str, dict[str, Any]] = {}
        for node, card in missing.items():
            task = provider.get_task(card)
            if not isinstance(task, Task) or card not in (task.id, task.raw_id) or task.binding_session_id:
                raise ExecutionOwnershipError("Provider Task 身份不匹配或已有执行绑定")
            prepared[card] = {"engine": "v2", "requirement_id": request.requirement_id,
                              "node_id": node, "aliases": sorted({task.id, task.raw_id or task.id})}
        # 与 V1 的 claim 共用全局 Workspace 锁：两边只有一方能先认领。
        with self.workspace.locked():
            claims = self._claims()
            for record in prepared.values():
                self._require_legacy_idle(request.requirement_id, set(record["aliases"]))
                for alias in record["aliases"]:
                    if alias in claims and claims[alias] != record:
                        raise ExecutionOwnershipError("Task 别名已被其他节点认领")
                    claims[alias] = record
            lease = self.store.acquire(f"ownership-{uuid4()}")
            try:
                self.store.mutate(lease, lambda data: data.update(claims=claims))
            finally:
                self.store.release(lease)
