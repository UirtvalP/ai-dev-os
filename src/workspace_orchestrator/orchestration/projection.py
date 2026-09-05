"""独立 Task Provider 投影账本；离线不阻止 Supervisor，投影永无 done 权限。"""

from __future__ import annotations

import copy
import math
import threading
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ..adapters.base import TaskProvider
from ..models import Task
from .contracts import fingerprint
from .store import OrchestrationStore, OrchestrationStoreError, SupervisorLease

_STATUS_MAP = {
    "pending": "todo", "dispatching": "in_progress", "running": "in_progress",
    "candidate": "in_review", "candidate_complete": "in_review", "verifying": "in_review",
    "accepted": "in_review", "blocked": "blocked", "unknown": "blocked", "stopped": "blocked",
    "replan_required": "blocked", "retired": "blocked", "failed": "blocked",
}


class TaskProjectionError(ValueError):
    """投影输入陈旧、账本不兼容或映射不安全；不得因此修改 Supervisor。"""


class TaskProjectionPump:
    """单线程、最新快照优先的异步投影，不让 Provider I/O 阻塞 Supervisor 续租。

    同一节点内容已经确认同步时忽略纯租约 revision 更新；离线结果不被标作已确认，
    下一次 submit 会再次唤醒同步。close(True) 只说明最终节点状态已获得 sync 的
    对齐结果且线程退出，不授予 Task/Gate 完成权限；超时或最终离线均返回 False。
    """

    def __init__(self, projector: TaskProjection) -> None:
        self.projector = projector
        self._condition = threading.Condition()
        self._accepting = True
        self._latest: tuple[dict[str, Any], str] | None = None
        self._last_submitted: tuple[dict[str, Any], str] | None = None
        self._last_synced_key: str | None = None
        self._last_error: str | None = None
        self._highest_revision = -1
        self._requirement_id: str | None = None
        self._node_versions: dict[str, tuple[int, str]] = {}
        self._thread = threading.Thread(target=self._run, name="task-projection", daemon=True)
        self._thread.start()

    @property
    def last_error(self) -> str | None:
        with self._condition:
            return self._last_error

    def submit(self, snapshot: dict[str, Any]) -> bool:
        """只复制/合并内存快照并唤醒线程；不调用 Provider，关闭或陈旧输入返回 False。"""

        try:
            copied = copy.deepcopy(snapshot)
            source = _source(copied)
        except Exception as exc:  # noqa: BLE001 - 输入隔离失败只拒绝快照，不中断 Supervisor。
            with self._condition:
                self._last_error = f"无法接收投影快照：{exc}"
            return False
        key = source["fingerprint"]
        with self._condition:
            if not self._accepting:
                return False
            if self._requirement_id not in (None, source["requirement_id"]):
                self._last_error = "投影泵不能跨 Requirement 复用"
                return False
            if (source["revision"] < self._highest_revision
                    or (source["revision"] == self._highest_revision
                        and self._last_submitted is not None and key != self._last_submitted[1])):
                self._last_error = "拒绝陈旧或同 revision 漂移的投影快照"
                return False
            for task_id, node in source["nodes"].items():
                previous = self._node_versions.get(task_id)
                if previous is not None and (
                    node["revision"] < previous[0]
                    or (node["revision"] == previous[0] and node["fingerprint"] != previous[1])
                ):
                    self._last_error = "拒绝陈旧或同 revision 漂移的节点快照"
                    return False
            self._highest_revision = source["revision"]
            self._requirement_id = source["requirement_id"]
            self._node_versions.update({task_id: (node["revision"], node["fingerprint"])
                                        for task_id, node in source["nodes"].items()})
            self._last_submitted = copied, key
            if key == self._last_synced_key:
                return True
            self._latest = copied, key
            self._condition.notify()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._latest is not None or not self._accepting)
                if self._latest is None:
                    return
                snapshot, key = self._latest
                self._latest = None
                if key == self._last_synced_key:
                    continue
            try:
                result = self.projector.sync(snapshot)
                if not isinstance(result, dict):
                    raise TaskProjectionError("投影返回了无效的结构化结果")
            except Exception as exc:  # noqa: BLE001 - 后台投影异常保留在 last_error，不影响本地执行。
                with self._condition:
                    self._last_error = f"后台投影失败：{exc}"
            else:
                with self._condition:
                    if result.get("status") == "synced":
                        self._last_synced_key = key
                    # Provider 离线/冲突的详细事实由独立投影账本保留。
                    self._last_error = None

    def close(self, timeout: float = 0) -> bool:
        """关闭接收，保留最后快照并有限等待；绝不杀掉控制面或伪造同步确认。"""

        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("投影关闭 timeout 必须是有限非负秒数")
        with self._condition:
            if self._accepting:
                self._accepting = False
                if self._last_submitted and self._last_submitted[1] != self._last_synced_key:
                    self._latest = self._last_submitted
                self._condition.notify()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        with self._condition:
            return not self._thread.is_alive() and (
                self._last_submitted is None or self._last_submitted[1] == self._last_synced_key
            )


class TaskProjection:
    """store 必须是调用方指定的独立控制面账本，不是 Supervisor 的 store。

    sync 应在 Supervisor 执行关键路径之外调用；只读取 snapshot，不认领执行租约。
    显式绑定授权从 todo 或已对齐状态建立基线。此后必须保持最后确认的 Provider
    version/status；无已记录写入意图的版本变化视为人工/其他写者接管，不抢回所有权。
    Provider 缺少幂等操作 ID：写入响应丢失后，可读确认目标已对齐但不能证明写者，
    因而停止后续覆盖（converged_unowned），不伪造拥有权或重复执行同一状态写入。
    """

    def __init__(
        self, store: OrchestrationStore, provider: TaskProvider, bindings: dict[str, str], *,
        ownership_check: Callable[[str, str, Task], None] | None = None,
    ) -> None:
        if not isinstance(bindings, dict) or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
            for key, value in bindings.items()
        ):
            raise TaskProjectionError("投影映射必须是非空 Task ID 到 Provider Task ID 的对象")
        if len(set(bindings.values())) != len(bindings):
            raise TaskProjectionError("多个本地节点不能共用一张 Provider 卡片")
        self.store, self.provider = store, provider
        self.bindings = dict(bindings)
        self.ownership_check = ownership_check

    def sync(self, supervisor_snapshot: dict[str, Any]) -> dict[str, Any]:
        """投影最新节点快照；每个节点每次最多一次 CAS，离线错误保留在独立账本。"""

        source = _source(supervisor_snapshot)
        existing = self.store.snapshot()["data"]
        _ledger(existing)
        # 初始安全检查必须先于 acquire，避免误传 Supervisor store 时改动其租约。
        self._validate_source(existing, source)
        lease = self.store.acquire(f"task-projection-{uuid4()}", ttl_seconds=60)
        try:
            def prepare(data: dict[str, Any]) -> None:
                _ledger(data)
                self._validate_source(data, source)
                data.update(kind="task-projection", projection_schema_version=1,
                            requirement_id=source["requirement_id"],
                            source_revision=source["revision"], source_fingerprint=source["fingerprint"])
                entries = data.setdefault("entries", {})
                for task_id, node in source["nodes"].items():
                    entry = entries.setdefault(task_id, {})
                    entry.update(provider_id=self.bindings.get(task_id), desired_revision=node["revision"],
                                 desired_fingerprint=node["fingerprint"], desired_status=node["status"])
                    entry.setdefault("state", "pending" if entry["provider_id"] else "unmapped")
                    entry.setdefault("ack", None)
                    entry.setdefault("pending", None)
                    entry.setdefault("last_error", None)
            prepared = self.store.mutate(lease, prepare)
            for task_id in source["nodes"]:
                self._sync_node(lease, task_id, prepared["data"]["entries"][task_id])
            result = self.store.snapshot()["data"]
            return {**copy.deepcopy(result), "status": "synced" if all(
                entry["state"] in ("acked", "converged_unowned")
                for entry in result["entries"].values()
            ) else "partial"}
        finally:
            # 过期/已被接管时，旧 holder 无权清掉后来者的租约；pending 留给下一轮读取。
            try:
                self.store.release(lease)
            except OrchestrationStoreError:
                pass

    def _validate_source(self, data: dict[str, Any], source: dict[str, Any]) -> None:
        if not data:
            return
        if data["requirement_id"] != source["requirement_id"]:
            raise TaskProjectionError("独立投影账本不能跨 Requirement 复用")
        if source["revision"] < data["source_revision"]:
            raise TaskProjectionError("拒绝陈旧的 Supervisor snapshot revision")
        if (source["revision"] == data["source_revision"]
                and source["fingerprint"] != data["source_fingerprint"]):
            raise TaskProjectionError("同一 Supervisor revision 的内容发生漂移")
        previous_bindings = {entry["provider_id"]: task_id for task_id, entry in data["entries"].items()
                             if entry["provider_id"] is not None}
        if any(provider_id in previous_bindings and previous_bindings[provider_id] != task_id
               for task_id, provider_id in self.bindings.items()):
            raise TaskProjectionError("已有卡片不能静默转移给其他节点")
        for task_id, node in source["nodes"].items():
            previous = data["entries"].get(task_id)
            if previous is None:
                continue
            provider_id = self.bindings.get(task_id)
            if previous["provider_id"] is not None and previous["provider_id"] != provider_id:
                raise TaskProjectionError("已有 Provider Task ID 映射不能静默移除或更换")
            if node["revision"] < previous["desired_revision"]:
                raise TaskProjectionError("拒绝陈旧的节点 revision")
            if (node["revision"] == previous["desired_revision"]
                    and node["fingerprint"] != previous["desired_fingerprint"]):
                raise TaskProjectionError("同一节点 revision 的源数据发生漂移")

    def _save(self, lease: SupervisorLease, task_id: str, **updates: Any) -> None:
        def change(data: dict[str, Any]) -> None:
            entry = data["entries"][task_id]
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(entry.get(key), dict):
                    entry[key].update(copy.deepcopy(value))
                else:
                    entry[key] = copy.deepcopy(value)
        self.store.mutate(lease, change)

    def _read_task(self, provider_id: str) -> Task:
        task = self.provider.get_task(provider_id)
        if not isinstance(task, Task) or provider_id not in (task.id, task.raw_id):
            raise TaskProjectionError("Provider 返回了错误的 Task 身份")
        if type(task.version) is not int or task.version < 0 or not isinstance(task.status, str):
            raise TaskProjectionError("Provider 缺少可靠的整数 version/status，不能安全投影")
        return task

    def _ack(
        self, lease: SupervisorLease, task_id: str, entry: dict[str, Any], task: Task, *, owned: bool
    ) -> None:
        self._save(lease, task_id, state="acked" if owned else "converged_unowned", pending=None,
                   last_error=None, ack={
                       "provider_version": task.version, "provider_status": task.status,
                       "node_revision": entry["desired_revision"],
                       "source_fingerprint": entry["desired_fingerprint"], "owned": owned,
                   })

    def _conflict(self, lease: SupervisorLease, task_id: str, task: Task, reason: str) -> None:
        self._save(lease, task_id, state="conflict", pending=None,
                   last_error={"code": "external_conflict", "message": reason},
                   observed={"provider_version": task.version, "provider_status": task.status})

    def _sync_node(self, lease: SupervisorLease, task_id: str, entry: dict[str, Any]) -> None:
        provider_id = entry["provider_id"]
        if provider_id is None:
            self._save(lease, task_id, state="unmapped", last_error={
                "code": "unmapped", "message": "未显式映射已有卡片；不创建可能重复的新卡",
            })
            return
        if entry["state"] == "conflict":
            # 冲突是持久的所有权丢失，不因离线或后续节点变化而自动重新认领。
            return
        try:
            current = self._read_task(provider_id)
        except Exception as exc:  # noqa: BLE001 - 可替换 Provider 的异常统一保留为离线事实。
            self._save(lease, task_id, state="offline", last_error={"code": "provider_unavailable", "message": str(exc)})
            return
        if entry["desired_status"] == "in_progress":
            try:
                if self.ownership_check is None:
                    raise TaskProjectionError("可执行面板状态需要可信的 V2 持久认领")
                self.ownership_check(self.store.snapshot()["data"]["requirement_id"], task_id, current)
            except Exception as exc:  # noqa: BLE001 -- 未认领不能制造 V1 可执行状态。
                self._save(lease, task_id, state="offline", last_error={
                    "code": "execution_unclaimed", "message": str(exc),
                })
                return
        assert current.version is not None
        if current.status == "done":
            self._conflict(lease, task_id, current, "done 属于最终 Gate，投影不得覆盖或降级")
            return
        pending, ack = entry.get("pending"), entry.get("ack")
        if pending is not None:
            if current.status == pending["target_status"] and current.version > pending["expected_version"]:
                if current.status == entry["desired_status"]:
                    self._ack(lease, task_id, entry, current, owned=False)
                else:
                    self._conflict(lease, task_id, current, "前次写入结果已变化但归属不明；不覆盖新目标")
                return
            if (current.version, current.status) != (pending["expected_version"], pending["expected_status"]):
                self._conflict(lease, task_id, current, "未完成写入期间发生外部状态变化")
                return
            # 版本仍未变化：原 CAS 即使稍后执行也与重试竞争同一 expected_version，
            # 至多一个会生效。先保存本次最新目标，绝不继续投影陈旧目标。
        elif ack is not None:
            if (current.version, current.status) != (ack["provider_version"], ack["provider_status"]):
                self._conflict(lease, task_id, current, "Provider 版本偏离最后确认状态，可能已被人工修改")
                return
            if not ack["owned"]:
                if current.status == entry["desired_status"]:
                    self._ack(lease, task_id, entry, current, owned=False)
                else:
                    self._conflict(lease, task_id, current, "前次响应丢失后无法证明写者，不重新认领状态所有权")
                return
        elif current.status not in ("todo", entry["desired_status"]):
            self._conflict(lease, task_id, current, "首次投影只允许从 todo 或已对齐状态建立基线")
            return
        if current.status == entry["desired_status"]:
            self._ack(lease, task_id, entry, current, owned=True)
            return
        assert current.version is not None
        intention = {
            "operation_id": str(uuid4()), "expected_version": current.version,
            "expected_status": current.status, "target_status": entry["desired_status"],
            "node_revision": entry["desired_revision"], "source_fingerprint": entry["desired_fingerprint"],
        }
        self._save(lease, task_id, state="pending", pending=intention, last_error=None)
        try:
            updated = self.provider.compare_and_set_status(
                provider_id, expected_version=current.version, expected_status=current.status,
                status=entry["desired_status"],
            )
        except Exception as exc:  # noqa: BLE001 - 写入结果不明时先只读恢复，不能传播或盲目重放。
            self._recover_cas(lease, task_id, entry, intention, str(exc))
            return
        if (not isinstance(updated, Task) or provider_id not in (updated.id, updated.raw_id)
                or type(updated.version) is not int or updated.version <= current.version
                or updated.status != entry["desired_status"]):
            self._recover_cas(lease, task_id, entry, intention, "Provider CAS 响应无法证明目标与新版本")
            return
        self._ack(lease, task_id, entry, updated, owned=True)

    def _recover_cas(
        self, lease: SupervisorLease, task_id: str, entry: dict[str, Any],
        pending: dict[str, Any], error: str,
    ) -> None:
        try:
            refreshed = self._read_task(entry["provider_id"])
        except Exception as exc:  # noqa: BLE001 - 外部重读失败保留 pending，不影响本地执行。
            self._save(lease, task_id, state="offline", last_error={
                "code": "write_outcome_unknown", "message": f"{error}; 重新读取失败：{exc}",
            })
            return
        assert refreshed.version is not None
        if refreshed.status == pending["target_status"] and refreshed.version > pending["expected_version"]:
            self._ack(lease, task_id, entry, refreshed, owned=False)
        elif (refreshed.version, refreshed.status) == (pending["expected_version"], pending["expected_status"]):
            self._save(lease, task_id, state="offline", last_error={"code": "write_not_confirmed", "message": error})
        else:
            self._conflict(lease, task_id, refreshed, "CAS 失败后读取到外部版本/状态变化；不重复覆盖")


def _source(snapshot: dict[str, Any]) -> dict[str, Any]:
    if (not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int
            or snapshot["schema_version"] != 1 or type(snapshot.get("revision")) is not int
            or snapshot["revision"] < 0 or not isinstance(snapshot.get("data"), dict)):
        raise TaskProjectionError("Supervisor snapshot envelope 格式无效")
    data = snapshot["data"]
    if not isinstance(data.get("requirement_id"), str) or not data["requirement_id"].strip():
        raise TaskProjectionError("Supervisor snapshot 缺少 Requirement 身份")
    if not isinstance(data.get("nodes"), dict):
        raise TaskProjectionError("Supervisor snapshot 缺少 nodes 对象")
    nodes: dict[str, Any] = {}
    for task_id, node in data["nodes"].items():
        if (not isinstance(task_id, str) or not task_id.strip() or not isinstance(node, dict)
                or type(node.get("revision")) is not int or node["revision"] < 1
                or not isinstance(node.get("status"), str) or node["status"] not in _STATUS_MAP):
            raise TaskProjectionError("节点 Task ID、revision 或状态无效；不允许投影 done")
        if isinstance(node.get("spec"), dict) and node["spec"].get("task_id") != task_id:
            raise TaskProjectionError("节点字典身份与 TaskSpec 身份不匹配")
        nodes[task_id] = {"revision": node["revision"], "fingerprint": fingerprint(node),
                          "status": _STATUS_MAP[node["status"]]}
    return {"requirement_id": data["requirement_id"], "revision": snapshot["revision"], "nodes": nodes,
            "fingerprint": fingerprint({"requirement_id": data["requirement_id"], "nodes": data["nodes"]})}


def _ledger(data: dict[str, Any]) -> None:
    if not data:
        return
    if (data.get("kind") != "task-projection" or type(data.get("projection_schema_version")) is not int
            or data["projection_schema_version"] != 1 or not isinstance(data.get("entries"), dict)
            or not isinstance(data.get("requirement_id"), str)
            or type(data.get("source_revision")) is not int or data["source_revision"] < 0
            or not isinstance(data.get("source_fingerprint"), str)):
        raise TaskProjectionError("传入 store 不是兼容的独立 Task 投影账本")
    for entry in data["entries"].values():
        if (not isinstance(entry, dict) or type(entry.get("desired_revision")) is not int
                or entry["desired_revision"] < 1 or not isinstance(entry.get("desired_fingerprint"), str)
                or entry.get("desired_status") not in set(_STATUS_MAP.values())
                or entry.get("state") not in ("pending", "unmapped", "acked", "offline", "conflict", "converged_unowned")
                or (entry.get("provider_id") is not None and not isinstance(entry["provider_id"], str))):
            raise TaskProjectionError("投影节点账本损坏")
        ack, pending = entry.get("ack"), entry.get("pending")
        if ack is not None and (
            not isinstance(ack, dict) or type(ack.get("provider_version")) is not int
            or ack["provider_version"] < 0 or not isinstance(ack.get("provider_status"), str)
            or type(ack.get("owned")) is not bool
        ):
            raise TaskProjectionError("投影确认版本无效")
        if pending is not None and (
            not isinstance(pending, dict) or type(pending.get("expected_version")) is not int
            or pending["expected_version"] < 0 or not isinstance(pending.get("expected_status"), str)
            or pending.get("target_status") not in set(_STATUS_MAP.values())
        ):
            raise TaskProjectionError("投影待确认写入意图无效")
