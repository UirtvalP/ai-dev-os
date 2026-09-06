"""复用 V1 生命周期时，禁止把 V2 的验证或合并候选当成交付完成。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
    """V2 只接受与 Phase 6 exact-SHA Gate 一致的持久 CompletionToken。"""
    if not is_v2_delivery(store, requirement_id):
        return
    workspace = store.path_for(requirement_id)
    token_path = workspace / "deployment" / "completion-token.json"
    gate_path = workspace / "phase-gates" / "phase-6.json"
    try:
        document = json.loads(token_path.read_text(encoding="utf-8"))
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            "V2 需求仍需完整交付 CompletionToken；Review/合并收据不能直接完成需求或触发部署"
        ) from exc
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("source"), str)
        or not isinstance(document.get("token"), dict)
        or not isinstance(document.get("environment_policy"), dict)
        or not isinstance(gate, dict)
    ):
        raise WorkspaceError("CompletionToken 缺失、损坏或与 Phase 6 Gate 不一致")
    token = document["token"]
    environment = document["environment_policy"]
    source_path = Path(document["source"])
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError("CompletionToken 的部署服务完成记录缺失或损坏") from exc
    if (
        source != token
        or not isinstance(gate, dict)
        or token.get("requirement_id") != requirement_id
        or not isinstance(token.get("token_id"), str)
        or not token["token_id"].startswith("completion-")
        or gate.get("status") != "PASS"
        or token.get("commit_sha") != gate.get("commit_sha")
        or type(token.get("deployment_required")) is not bool
        or environment.get("name") != token.get("environment")
        or environment.get("deployment_required") != token.get("deployment_required")
        or (token["deployment_required"] and not token.get("deployment_receipt_id"))
        or (not token["deployment_required"] and token.get("deployment_receipt_id") is not None)
    ):
        raise WorkspaceError("CompletionToken 缺失、损坏或与 Phase 6 Gate 不一致")
