"""独立任务投影账本的离线、丢响应、CAS 冲突与人工状态保护测试。"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from workspace_orchestrator.adapters.base import TaskProvider, TaskProviderError
from workspace_orchestrator.models import Task
from workspace_orchestrator.orchestration.projection import (
    TaskProjection,
    TaskProjectionError,
    TaskProjectionPump,
)
from workspace_orchestrator.orchestration.store import OrchestrationStore, OrchestrationStoreError


class FakeProvider:
    def __init__(self, status: str = "todo", version: int = 1) -> None:
        self.task = Task("P1", "已有卡片", status=status, version=version)
        self.online = True
        self.lose_reply = False
        self.fail_read_after_write = False
        self.before_cas: Callable[[], None] | None = None
        self.attempts = 0
        self.writes = 0
        self._lock = threading.Lock()

    def get_task(self, task_id: str) -> Task:
        with self._lock:
            if not self.online:
                raise TaskProviderError("Provider 离线")
            if task_id != "P1":
                raise TaskProviderError("不存在的卡片")
            return replace(self.task)

    def compare_and_set_status(
        self, task_id: str, *, expected_version: int, expected_status: str, status: str
    ) -> Task:
        self.attempts += 1
        if self.before_cas:
            self.before_cas()
        with self._lock:
            if not self.online:
                raise TaskProviderError("Provider 离线")
            if self.task.version != expected_version or self.task.status != expected_status:
                raise TaskProviderError("CAS 版本冲突")
            assert task_id == "P1" and status != "done"
            self.task = replace(self.task, status=status, version=expected_version + 1)
            self.writes += 1
            if self.fail_read_after_write:
                self.online = False
            if self.lose_reply:
                raise TaskProviderError("写入响应丢失")
            return replace(self.task)

    def create_task(self, *args: object, **kwargs: object) -> Task:
        raise AssertionError("投影不得创建新卡")

    def manual(self, status: str) -> None:
        with self._lock:
            assert self.task.version is not None
            self.task = replace(self.task, status=status, version=self.task.version + 1)


def snapshot(revision: int, node_revision: int, status: str) -> dict[str, Any]:
    return {"schema_version": 1, "revision": revision, "data": {
        "requirement_id": "REQ-020", "nodes": {"T1": {
            "revision": node_revision, "status": status, "spec": {"task_id": "T1"},
        }},
    }}


def projection(tmp_path: Path, provider: FakeProvider, **kwargs: Any) -> TaskProjection:
    return TaskProjection(OrchestrationStore(tmp_path / "projection"), cast(TaskProvider, provider),
                          kwargs.get("bindings", {"T1": "P1"}))


def test_offline_local_source_advances_and_recovery_writes_only_latest_target(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.online = False
    project = projection(tmp_path, provider)
    older = snapshot(1, 1, "pending")
    assert project.sync(older)["entries"]["T1"]["state"] == "offline"
    latest = snapshot(3, 3, "candidate_complete")
    original = copy.deepcopy(latest)
    offline = project.sync(latest)
    assert offline["entries"]["T1"]["desired_status"] == "in_review"
    assert latest == original and provider.writes == 0
    provider.online = True
    restored = project.sync(latest)
    assert restored["status"] == "synced" and provider.task.status == "in_review"
    assert provider.writes == 1
    assert project.sync(latest)["entries"]["T1"]["state"] == "acked"
    assert provider.attempts == 1


@pytest.mark.parametrize("local,status", [
    ("pending", "todo"), ("running", "in_progress"), ("dispatching", "in_progress"),
    ("candidate_complete", "in_review"), ("accepted", "in_review"), ("verifying", "in_review"),
    ("blocked", "blocked"), ("unknown", "blocked"), ("stopped", "blocked"),
    ("replan_required", "blocked"), ("retired", "blocked"),
])
def test_projection_status_mapping_never_grants_done(tmp_path: Path, local: str, status: str) -> None:
    provider = FakeProvider()
    result = projection(tmp_path, provider).sync(snapshot(1, 1, local))
    assert provider.task.status == status and result["entries"]["T1"]["state"] == "acked"
    assert status != "done"


def test_owned_projection_moves_forward_only_when_last_ack_version_is_unchanged(tmp_path: Path) -> None:
    provider = FakeProvider()
    project = projection(tmp_path, provider)
    project.sync(snapshot(1, 1, "pending"))
    project.sync(snapshot(2, 2, "running"))
    complete = project.sync(snapshot(3, 3, "accepted"))
    assert provider.writes == 2 and provider.task.status == "in_review"
    assert complete["entries"]["T1"]["ack"]["provider_version"] == provider.task.version


def test_lost_write_response_read_confirmation_prevents_duplicate_cas(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.lose_reply = True
    project = projection(tmp_path, provider)
    source = snapshot(1, 1, "running")
    result = project.sync(source)
    assert result["entries"]["T1"]["state"] == "converged_unowned"
    assert result["entries"]["T1"]["ack"]["owned"] is False
    assert provider.writes == 1
    assert projection(tmp_path, provider).sync(source)["status"] == "synced"
    assert provider.attempts == 1
    # 没有 Provider 操作 ID，就不能把碰巧对齐状态当作未来覆盖权限。
    result = project.sync(snapshot(2, 2, "accepted"))
    assert result["entries"]["T1"]["state"] == "conflict"
    assert provider.task.status == "in_progress" and provider.writes == 1


def test_lost_reply_and_offline_read_recover_pending_after_restart(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.lose_reply, provider.fail_read_after_write = True, True
    project = projection(tmp_path, provider)
    source = snapshot(1, 1, "running")
    first = project.sync(source)["entries"]["T1"]
    assert first["state"] == "offline" and first["pending"]
    provider.online = True
    recovered = projection(tmp_path, provider).sync(source)["entries"]["T1"]
    assert recovered["state"] == "converged_unowned" and recovered["pending"] is None
    assert provider.writes == provider.attempts == 1


def test_failed_cas_read_rechecks_and_does_not_overwrite_manual_change(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.before_cas = lambda: provider.manual("blocked")
    project = projection(tmp_path, provider)
    result = project.sync(snapshot(1, 1, "running"))
    assert result["entries"]["T1"]["state"] == "conflict"
    assert provider.task.status == "blocked" and provider.writes == 0 and provider.attempts == 1
    provider.online = False
    project.sync(snapshot(2, 2, "accepted"))
    provider.online = True
    provider.before_cas = None
    project.sync(snapshot(3, 3, "running"))
    assert provider.attempts == 1  # 离线及后续本地变化不能解除人工冲突。


def test_same_status_with_new_manual_version_does_not_reclaim_ownership(tmp_path: Path) -> None:
    provider = FakeProvider()
    project = projection(tmp_path, provider)
    project.sync(snapshot(1, 1, "running"))
    provider.manual("in_progress")
    result = project.sync(snapshot(2, 2, "accepted"))
    assert result["entries"]["T1"]["state"] == "conflict"
    assert provider.task.status == "in_progress" and provider.writes == 1


def test_gate_done_and_existing_unowned_active_card_are_never_downgraded(tmp_path: Path) -> None:
    provider = FakeProvider("done")
    result = projection(tmp_path, provider).sync(snapshot(1, 1, "running"))
    assert result["entries"]["T1"]["state"] == "conflict" and provider.writes == 0
    other = FakeProvider("in_progress")
    result = projection(tmp_path / "other", other).sync(snapshot(1, 1, "pending"))
    assert result["entries"]["T1"]["state"] == "conflict" and other.writes == 0


def test_unmapped_nodes_never_create_cards_and_late_explicit_mapping_is_allowed(tmp_path: Path) -> None:
    provider = FakeProvider()
    source = snapshot(1, 1, "running")
    unmapped = projection(tmp_path, provider, bindings={}).sync(source)
    assert unmapped["entries"]["T1"]["state"] == "unmapped" and provider.attempts == 0
    mapped = projection(tmp_path, provider).sync(source)
    assert mapped["entries"]["T1"]["state"] == "acked" and provider.writes == 1
    with pytest.raises(TaskProjectionError):
        projection(tmp_path, provider, bindings={"T1": "another"}).sync(source)
    with pytest.raises(TaskProjectionError):
        projection(tmp_path, provider, bindings={"T1": "P1", "T2": "P1"})


def test_old_snapshot_node_revision_and_equal_revision_drift_are_rejected(tmp_path: Path) -> None:
    provider = FakeProvider()
    project = projection(tmp_path, provider)
    project.sync(snapshot(5, 3, "running"))
    with pytest.raises(TaskProjectionError, match="snapshot revision"):
        project.sync(snapshot(4, 2, "pending"))
    with pytest.raises(TaskProjectionError, match="节点 revision"):
        project.sync(snapshot(6, 2, "pending"))
    with pytest.raises(TaskProjectionError, match="漂移"):
        project.sync(snapshot(5, 3, "accepted"))
    with pytest.raises(TaskProjectionError, match="漂移"):
        project.sync(snapshot(6, 3, "accepted"))
    assert provider.writes == 1


def test_projection_unknown_ledger_fields_are_preserved(tmp_path: Path) -> None:
    provider = FakeProvider()
    project = projection(tmp_path, provider)
    project.sync(snapshot(1, 1, "pending"))
    lease = project.store.acquire("test-extension")

    def add_fields(data: dict[str, Any]) -> None:
        data["future"] = {"keep": [1, 2]}
        data["entries"]["T1"]["future_node"] = "preserve"
        data["entries"]["T1"]["ack"]["future_ack"] = "preserve"

    project.store.mutate(lease, add_fields)
    project.store.release(lease)
    synced = project.sync(snapshot(2, 2, "running"))
    assert synced["future"] == {"keep": [1, 2]}
    assert synced["entries"]["T1"]["future_node"] == "preserve"
    assert synced["entries"]["T1"]["ack"]["future_ack"] == "preserve"


def test_concurrent_syncs_do_not_double_write(tmp_path: Path) -> None:
    provider = FakeProvider()
    entered, release = threading.Event(), threading.Event()

    def before_cas() -> None:
        entered.set()
        assert release.wait(3)

    provider.before_cas = before_cas
    first, second = projection(tmp_path, provider), projection(tmp_path, provider)
    source = snapshot(1, 1, "running")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.sync, source)
        assert entered.wait(3)
        try:
            with pytest.raises(OrchestrationStoreError, match="holder"):
                second.sync(source)
        finally:
            release.set()
        assert running.result(timeout=3)["status"] == "synced"
    assert second.sync(source)["status"] == "synced" and provider.writes == provider.attempts == 1


def test_cannot_accidentally_take_over_supervisor_store(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path / "supervisor")
    lease = store.acquire("supervisor")
    store.mutate(lease, lambda data: data.update(snapshot(1, 1, "running")["data"]))
    before = store.snapshot()
    provider = FakeProvider()
    with pytest.raises(TaskProjectionError, match="独立"):
        TaskProjection(store, cast(TaskProvider, provider), {"T1": "P1"}).sync(snapshot(1, 1, "running"))
    assert store.snapshot() == before and provider.writes == 0
    store.release(lease)


class BlockingProjector:
    """用 Event 表示真实网络阻塞，不通过轮询或睡眠驱动测试。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.observed: list[dict[str, Any]] = []
        self.active = 0
        self.maximum_active = 0
        self.status = "synced"

    def sync(self, source: dict[str, Any]) -> dict[str, Any]:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            self.observed.append(copy.deepcopy(source))
            if len(self.observed) == 1:
                self.entered.set()
                assert self.release.wait(3)
            return {"status": self.status}
        finally:
            self.active -= 1


def test_pump_submit_is_nonblocking_and_coalesces_intermediate_snapshots() -> None:
    projector = BlockingProjector()
    pump = TaskProjectionPump(cast(TaskProjection, projector))
    try:
        assert pump.submit(snapshot(1, 1, "pending"))
        assert projector.entered.wait(2)
        started = time.monotonic()
        assert pump.submit(snapshot(2, 2, "running"))
        latest = snapshot(3, 3, "accepted")
        assert pump.submit(latest)
        assert time.monotonic() - started < 0.5
        latest["data"]["nodes"]["T1"]["status"] = "blocked"
    finally:
        projector.release.set()
        assert pump.close(timeout=3)
    assert [item["data"]["nodes"]["T1"]["status"] for item in projector.observed] == ["pending", "accepted"]
    assert projector.maximum_active == 1


def test_pump_close_is_bounded_keeps_final_snapshot_and_rejects_new_submissions() -> None:
    projector = BlockingProjector()
    pump = TaskProjectionPump(cast(TaskProjection, projector))
    assert pump.submit(snapshot(1, 1, "pending"))
    assert projector.entered.wait(2)
    assert pump.submit(snapshot(2, 2, "candidate_complete"))
    started = time.monotonic()
    try:
        assert not pump.close(timeout=0.01)
        assert time.monotonic() - started < 0.5
        assert not pump.submit(snapshot(3, 3, "accepted"))
    finally:
        projector.release.set()
        assert pump.close(timeout=3)
    assert projector.observed[-1]["data"]["nodes"]["T1"]["status"] == "candidate_complete"


def test_pump_ignores_lease_only_changes_without_dropping_latest_nodes() -> None:
    projector = BlockingProjector()
    pump = TaskProjectionPump(cast(TaskProjection, projector))
    try:
        pump.submit(snapshot(1, 1, "pending"))
        assert projector.entered.wait(2)
        for revision in range(2, 12):
            assert pump.submit(snapshot(revision, 1, "pending"))
        projector.release.set()
        assert pump.close(timeout=3)
    finally:
        projector.release.set()
        pump.close(timeout=3)
    assert len(projector.observed) == 1


def test_pump_errors_do_not_escape_or_falsely_ack_final_state() -> None:
    class FailingProjector:
        def sync(self, source: dict[str, Any]) -> dict[str, Any]:
            raise OSError("ledger unavailable")

    pump = TaskProjectionPump(cast(TaskProjection, FailingProjector()))
    assert pump.submit(snapshot(1, 1, "running"))
    assert not pump.close(timeout=3)
    assert pump.last_error and "ledger unavailable" in pump.last_error


def test_pump_offline_result_is_not_a_successful_close_and_retries_on_submission(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.online = False
    projector = projection(tmp_path, provider)
    pump = TaskProjectionPump(projector)
    pump.submit(snapshot(1, 1, "running"))
    assert not pump.close(timeout=3)
    assert projector.store.snapshot()["data"]["entries"]["T1"]["state"] == "offline"
    provider.online = True
    resumed = TaskProjectionPump(projector)
    assert resumed.submit(snapshot(2, 1, "running"))
    assert resumed.close(timeout=3)
    assert provider.writes == 1


def test_pump_rejects_stale_or_cross_requirement_input_without_losing_final_state() -> None:
    projector = BlockingProjector()
    pump = TaskProjectionPump(cast(TaskProjection, projector))
    try:
        assert pump.submit(snapshot(4, 2, "running"))
        assert projector.entered.wait(2)
        assert pump.submit(snapshot(5, 3, "candidate_complete"))
        assert not pump.submit(snapshot(3, 1, "pending"))
        assert not pump.submit(snapshot(6, 2, "running"))
        other = snapshot(6, 4, "accepted")
        other["data"]["requirement_id"] = "REQ-OTHER"
        assert not pump.submit(other)
    finally:
        projector.release.set()
        assert pump.close(timeout=3)
    assert projector.observed[-1]["revision"] == 5
