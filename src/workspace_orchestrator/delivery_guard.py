"""复用 V1 生命周期时，禁止把 V2 的验证或合并候选当成交付完成。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .orchestration.store import OrchestrationStore
from .workspace import WorkspaceError, WorkspaceStore


def mark_v2_delivery(store: WorkspaceStore, requirement_id: str) -> None:
    """与旧完成副作用共用 Requirement 短锁；已完成需求不能静默转为 V2。"""
    with store.locked(requirement_id):
        meta = store.load(requirement_id)["meta"]
        if meta.get("status") == "done":
            raise WorkspaceError("已完成 Requirement 不能接入新的 V2 执行；请使用新需求")
        if meta.get("delivery_profile") != "v2":
            store.touch_meta(requirement_id, delivery_profile="v2")


@contextmanager
def delivery_completion_guard(store: WorkspaceStore, requirement_id: str) -> Iterator[None]:
    """仅包围完成写入，不持锁运行验证；V2 接入不能穿越最终检查与副作用。"""
    with store.locked(requirement_id):
        require_delivery_completion(store, requirement_id)
        yield


def is_v2_delivery(store: WorkspaceStore, requirement_id: str) -> bool:
    if store.load(requirement_id)["meta"].get("delivery_profile") == "v2":
        return True
    # 兼容 Phase 2 已持久计划；不能靠删除新增标记使既有 V2 需求退回旧完成入口。
    data = OrchestrationStore(
        store.path_for(requirement_id) / "orchestration" / "supervisor",
    ).snapshot()["data"]
    return bool(data.get("plan"))


def require_delivery_completion(store: WorkspaceStore, requirement_id: str) -> None:
    """Phase 3 不签发 CompletionToken；后续完成门禁在此替换，不提供布尔旁路。"""
    if is_v2_delivery(store, requirement_id):
        raise WorkspaceError(
            "V2 需求仍需完整交付 CompletionToken；Review/合并收据不能直接完成需求或触发部署"
        )
