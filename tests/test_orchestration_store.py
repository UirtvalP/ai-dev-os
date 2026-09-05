"""Supervisor 本地状态的租约、并发和崩溃边界。"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from workspace_orchestrator.orchestration.store import (
    OrchestrationStore,
    OrchestrationStoreError,
    SupervisorLease,
)


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_epoch_guard_rechecks_expiry_without_mutating_revision(tmp_path):
    clock = Clock()
    store = OrchestrationStore(tmp_path / "guard", clock=clock)
    lease = store.acquire("holder", ttl_seconds=5)
    revision = store.snapshot()["revision"]
    with store.guard_epoch("holder", lease.fence) as check:
        check()
        clock.value = 106
        with pytest.raises(OrchestrationStoreError):
            check()
    assert store.snapshot()["revision"] == revision
    newer = store.acquire("new-holder")
    with pytest.raises(OrchestrationStoreError), store.guard_epoch("holder", lease.fence):
        pytest.fail("旧 epoch 不得取得外部启动许可")
    with store.guard_epoch("new-holder", newer.fence) as check:
        check()


def test_epoch_guard_serializes_takeover_until_start_boundary_exits(tmp_path):
    import threading

    clock = Clock()
    first = OrchestrationStore(tmp_path / "guard", clock=clock)
    lease = first.acquire("old", ttl_seconds=5)
    second = OrchestrationStore(first.root, clock=clock)
    waiting = threading.Event()
    def takeover():
        waiting.set()
        return second.acquire("new")
    with ThreadPoolExecutor(1) as pool:
        with first.guard_epoch("old", lease.fence) as check:
            clock.value = 106
            future = pool.submit(takeover)
            assert waiting.wait(3)
            with pytest.raises(OrchestrationStoreError):
                check()
            assert not future.done()
        assert future.result(timeout=5).fence == lease.fence + 1


@pytest.mark.parametrize("mode,attributes", [(stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o600, 0x400)])
def test_redirect_error_classification_without_platform_link_privileges(tmp_path, monkeypatch, mode, attributes):
    store = OrchestrationStore(tmp_path / "control")
    target = store.root / "state.lock"
    original = Path.lstat
    def observed(path):
        if path == target:
            return SimpleNamespace(st_mode=mode, st_nlink=1, st_file_attributes=attributes)
        return original(path)
    monkeypatch.setattr(Path, "lstat", observed)
    with pytest.raises(OrchestrationStoreError, match="重定向"):
        store._check_file(target)


def _state_path(root: Path) -> Path:
    return root / "state.json"


def _read(root: Path) -> dict[str, Any]:
    return json.loads(_state_path(root).read_text(encoding="utf-8"))


def _write(root: Path, document: dict[str, Any]) -> None:
    _state_path(root).write_text(json.dumps(document), encoding="utf-8")


def _increment(data: dict[str, Any]) -> None:
    data["count"] = data.get("count", 0) + 1


def _compete(root: str, owner: str, start: Any, results: Any) -> None:
    if not start.wait(15):
        raise RuntimeError("并发租约测试启动超时")
    try:
        lease = OrchestrationStore(Path(root), clock=Clock()).acquire(owner)
        results.put((owner, lease.fence))
    except OrchestrationStoreError:
        results.put((owner, None))


def _process_mutate(root: str, lease: SupervisorLease, start: Any) -> None:
    if not start.wait(15):
        raise RuntimeError("并发变更测试启动超时")
    store = OrchestrationStore(Path(root), clock=Clock())
    for _ in range(12):
        store.mutate(lease, _increment)


def _crash_before_replace(root: str, lease: SupervisorLease) -> None:
    store = OrchestrationStore(Path(root), clock=Clock())

    def crash(source: Any, destination: Any) -> None:
        os._exit(23)

    os.replace = crash
    store.mutate(lease, _increment)


def _join(processes: list[Any]) -> None:
    try:
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_snapshot_has_no_filesystem_side_effects_and_initial_counters(tmp_path: Path) -> None:
    root = tmp_path / "not-created" / "control"
    store = OrchestrationStore(root, clock=Clock())
    initial = store.snapshot()
    assert initial == {
        "schema_version": 1, "revision": 0, "fence": 0, "lease": None,
        "last_observed_at": 0.0, "data": {},
    }
    assert not root.parent.exists()
    initial["data"]["bad"] = True
    assert store.snapshot()["data"] == {}
    existing = OrchestrationStore(tmp_path, clock=Clock())
    existing.snapshot()
    assert list(tmp_path.iterdir()) == []


def test_acquire_mutate_renew_release_and_fences(tmp_path: Path) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    first = store.acquire("first")
    assert first == SupervisorLease("first", 1, 130.0)
    assert store.snapshot()["revision"] == 1
    with pytest.raises(FrozenInstanceError):
        first.fence = 50  # type: ignore[misc]
    changed = store.mutate(first, _increment, expected_revision=1)
    assert changed["data"] == {"count": 1}
    assert changed["revision"] == 2
    changed["data"]["count"] = 99
    assert store.snapshot()["data"]["count"] == 1
    clock.value = 105
    renewed = store.renew(first, ttl_seconds=60)
    assert renewed == SupervisorLease("first", 1, 165.0)
    assert store.snapshot()["revision"] == 3
    store.release(renewed)
    assert store.snapshot()["lease"] is None
    assert store.snapshot()["revision"] == 4
    assert store.acquire("second").fence == 2
    assert store.snapshot()["revision"] == 5


def test_persisted_snapshot_does_not_modify_files(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    store.acquire("holder")
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in tmp_path.iterdir()}
    store.snapshot()
    after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
             for path in tmp_path.iterdir()}
    assert after == before


def test_snapshot_does_not_create_a_missing_lock_for_existing_state(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    store.acquire("holder")
    (tmp_path / "state.lock").unlink()
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError, match="锁文件"):
        store.snapshot()
    assert not (tmp_path / "state.lock").exists()
    assert _state_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("owner", ["first", "different"])
def test_active_holder_cannot_be_reacquired_even_by_same_owner(tmp_path: Path, owner: str) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    store.acquire("first")
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError, match="holder"):
        store.acquire(owner)
    assert _state_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("operation", ["mutate", "renew", "release"])
def test_renewed_lease_invalidates_old_credentials(tmp_path: Path, operation: str) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    first = store.acquire("owner")
    clock.value += 1
    renewed = store.renew(first)
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError, match="失效|fence"):
        if operation == "mutate":
            store.mutate(first, _increment)
        else:
            getattr(store, operation)(first)
    assert _state_path(tmp_path).read_bytes() == before
    store.mutate(renewed, _increment)


@pytest.mark.parametrize("operation", ["mutate", "renew", "release"])
@pytest.mark.parametrize("replace_holder", [False, True])
def test_expired_holder_cannot_mutate_or_release_successor(
    tmp_path: Path, operation: str, replace_holder: bool
) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    old = store.acquire("old", 1)
    clock.value = 101
    if replace_holder:
        current = OrchestrationStore(tmp_path, clock=clock).acquire("new")
        assert current.fence == 2
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError, match="过期|失效|fence"):
        if operation == "mutate":
            store.mutate(old, _increment)
        else:
            getattr(store, operation)(old)
    assert _state_path(tmp_path).read_bytes() == before


def test_expiry_during_mutator_rolls_back_all_changes(tmp_path: Path) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    lease = store.acquire("holder", 1)
    before = _state_path(tmp_path).read_bytes()

    def slow(data: dict[str, Any]) -> None:
        data["partial"] = True
        clock.value = 101

    with pytest.raises(OrchestrationStoreError, match="过期"):
        store.mutate(lease, slow)
    assert _state_path(tmp_path).read_bytes() == before
    assert list(tmp_path.glob(".state-*.tmp")) == []


def test_expiry_during_fsync_cannot_replace_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    lease = store.acquire("holder", 1)
    before = _state_path(tmp_path).read_bytes()
    real_fsync = os.fsync

    def delayed_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        clock.value = 101

    monkeypatch.setattr(os, "fsync", delayed_fsync)
    with pytest.raises(OrchestrationStoreError, match="过期"):
        store.mutate(lease, _increment)
    assert _state_path(tmp_path).read_bytes() == before


def test_mutator_exception_and_revision_conflict_leave_original_bytes(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    before = _state_path(tmp_path).read_bytes()

    def fail(data: dict[str, Any]) -> None:
        data["partial"] = True
        raise RuntimeError("模拟 mutator 失败")

    with pytest.raises(RuntimeError, match="mutator"):
        store.mutate(lease, fail)
    with pytest.raises(OrchestrationStoreError, match="revision"):
        store.mutate(lease, _increment, expected_revision=0)
    assert _state_path(tmp_path).read_bytes() == before


def test_reentrant_mutator_cannot_overwrite_nested_commit(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")

    def reentrant(data: dict[str, Any]) -> None:
        data["outer"] = "must not commit"
        store.mutate(lease, lambda nested: nested.update(inner="keep"))

    with pytest.raises(OrchestrationStoreError, match="revision"):
        store.mutate(lease, reentrant)
    assert store.snapshot()["data"] == {"inner": "keep"}
    assert store.snapshot()["revision"] == 2


def test_caller_cannot_keep_mutable_reference_to_committed_data(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    kept: list[dict[str, Any]] = []

    def change(data: dict[str, Any]) -> None:
        data["items"] = [1]
        kept.append(data)

    snapshot = store.mutate(lease, change)
    kept[0]["items"].append(2)
    snapshot["data"]["items"].append(3)
    assert store.snapshot()["data"] == {"items": [1]}


def test_unknown_envelope_data_and_lease_fields_survive_renew_and_mutate(tmp_path: Path) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    lease = store.acquire("holder")
    document = _read(tmp_path)
    document["future_envelope"] = {"flags": [True, None]}
    document["lease"]["future_lease"] = "retain"
    document["data"]["future_data"] = ["keep"]
    _write(tmp_path, document)
    clock.value = 101
    renewed = store.renew(lease)
    state = store.mutate(renewed, _increment)
    assert state["future_envelope"] == {"flags": [True, None]}
    assert state["lease"]["future_lease"] == "retain"
    assert state["data"]["future_data"] == ["keep"]


@pytest.mark.parametrize("ttl", [0, -1, True, False, "30", None, float("nan"), float("inf"),
                                  -float("inf")])
def test_invalid_ttl_is_rejected_before_creating_directory(tmp_path: Path, ttl: Any) -> None:
    root = tmp_path / "absent"
    with pytest.raises(OrchestrationStoreError, match="ttl_seconds"):
        OrchestrationStore(root, clock=Clock()).acquire("holder", ttl)
    assert not root.exists()


@pytest.mark.parametrize("owner", ["", "  ", None, 5, True])
def test_invalid_owner_is_rejected_without_files(tmp_path: Path, owner: Any) -> None:
    with pytest.raises(OrchestrationStoreError, match="owner"):
        OrchestrationStore(tmp_path, clock=Clock()).acquire(owner)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("revision", [-1, True, "1", 1.5])
def test_expected_revision_is_strict_integer(tmp_path: Path, revision: Any) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError, match="expected_revision"):
        store.mutate(lease, _increment, expected_revision=revision)
    assert _state_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("value", [-1, True, "100", None, float("nan"), float("inf")])
def test_invalid_clock_fails_closed(tmp_path: Path, value: Any) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    lease = store.acquire("holder")
    before = _state_path(tmp_path).read_bytes()
    clock.value = value
    with pytest.raises(OrchestrationStoreError, match="clock"):
        store.mutate(lease, _increment)
    assert _state_path(tmp_path).read_bytes() == before


def test_backward_clock_survives_restart_and_is_checked_after_mutation(tmp_path: Path) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    lease = store.acquire("holder")
    clock.value = 105
    store.mutate(lease, _increment)
    before = _state_path(tmp_path).read_bytes()
    clock.value = 104
    with pytest.raises(OrchestrationStoreError, match="时钟倒退"):
        OrchestrationStore(tmp_path, clock=clock).acquire("other")
    clock.value = 106

    def backwards(data: dict[str, Any]) -> None:
        data["must_not_commit"] = True
        clock.value = 105

    with pytest.raises(OrchestrationStoreError, match="时钟倒退"):
        store.mutate(lease, backwards)
    assert _state_path(tmp_path).read_bytes() == before


def test_snapshot_remembers_clock_high_water_without_writing(tmp_path: Path) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    store.snapshot()
    clock.value = 99
    with pytest.raises(OrchestrationStoreError, match="时钟倒退"):
        store.snapshot()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw", [
    b"{", b"\xff", b"[]", b'{"schema_version":2}',
    b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}',
    b'{"future":{"x":1,"x":2}}', b'{"future":1e999}', b'{"future":-1e999}',
])
def test_corrupt_or_ambiguous_json_preserves_bytes(tmp_path: Path, raw: bytes) -> None:
    _state_path(tmp_path).write_bytes(raw)
    store = OrchestrationStore(tmp_path, clock=Clock())
    with pytest.raises(OrchestrationStoreError):
        store.snapshot()
    with pytest.raises(OrchestrationStoreError):
        store.acquire("holder")
    assert _state_path(tmp_path).read_bytes() == raw
    assert list(tmp_path.glob(".state-*.tmp")) == []


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"revision": True}, {"revision": -1}, {"revision": 0},
    {"fence": -1}, {"data": []}, {"last_observed_at": -1},
    {"lease": []}, {"lease": {}}, {"lease": {"owner": "x", "fence": 2, "expires_at": 130}},
])
def test_invalid_envelope_is_not_reset(tmp_path: Path, change: dict[str, Any]) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    store.acquire("holder")
    document = _read(tmp_path)
    document.update(change)
    _write(tmp_path, document)
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError):
        store.acquire("other")
    assert _state_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("value", [float("nan"), object(), {1: "bad key"}, (1, 2)])
def test_non_json_mutation_cannot_corrupt_state(tmp_path: Path, value: Any) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError):
        store.mutate(lease, lambda data: data.update(value=value))
    assert _state_path(tmp_path).read_bytes() == before


def test_nonfinite_unknown_field_in_complete_state_is_preserved(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    store.acquire("holder")
    document = _read(tmp_path)
    document["future"] = "OVERFLOW"
    raw = json.dumps(document).replace('"OVERFLOW"', "1e999").encode("utf-8")
    _state_path(tmp_path).write_bytes(raw)
    with pytest.raises(OrchestrationStoreError, match="有限浮点"):
        store.snapshot()
    assert _state_path(tmp_path).read_bytes() == raw


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failed_atomic_write_preserves_original_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    before = _state_path(tmp_path).read_bytes()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("模拟原子写失败")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(OrchestrationStoreError, match="原子写失败"):
        store.mutate(lease, _increment)
    assert _state_path(tmp_path).read_bytes() == before
    assert list(tmp_path.glob(".state-*.tmp")) == []


def test_fsync_precedes_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    operations: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor: int) -> None:
        operations.append("fsync")
        real_fsync(descriptor)

    def replace_file(source: Any, target: Any) -> None:
        operations.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace_file)
    store.mutate(lease, _increment)
    assert operations[:2] == ["fsync", "replace"]
    if os.name != "nt":
        assert operations == ["fsync", "replace", "fsync"]


@pytest.mark.parametrize("file", ["state.json", "state.lock"])
def test_directory_cannot_impersonate_state_file(tmp_path: Path, file: str) -> None:
    (tmp_path / file).mkdir()
    with pytest.raises(OrchestrationStoreError, match="独立普通文件"):
        OrchestrationStore(tmp_path, clock=Clock()).acquire("holder")


@pytest.mark.parametrize("file", ["state.json", "state.lock"])
def test_hardlinked_files_cannot_modify_another_file(tmp_path: Path, file: str) -> None:
    root = tmp_path / "control"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    os.link(outside, root / file)
    with pytest.raises(OrchestrationStoreError, match="独立普通文件"):
        OrchestrationStore(root, clock=Clock()).acquire("holder")
    assert outside.read_bytes() == b"preserve"


@pytest.mark.parametrize("file", ["state.json", "state.lock", "root"])
def test_symlink_paths_are_rejected_even_before_initialization(tmp_path: Path, file: str) -> None:
    root = tmp_path / "control"
    outside = tmp_path / "outside"
    try:
        if file == "root":
            outside.mkdir()
            root.symlink_to(outside, target_is_directory=True)
        else:
            root.mkdir()
            outside.write_bytes(b"preserve")
            (root / file).symlink_to(outside)
    except OSError:
        pytest.skip("当前系统未授权创建符号链接")
    with pytest.raises(OrchestrationStoreError, match="重定向"):
        OrchestrationStore(root, clock=Clock()).acquire("holder")
    if file != "root":
        assert outside.read_bytes() == b"preserve"


def test_parallel_threads_do_not_lose_mutations(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    with ThreadPoolExecutor(max_workers=8) as executor:
        snapshots = list(executor.map(lambda _: store.mutate(lease, _increment), range(24)))
    assert sorted(item["revision"] for item in snapshots) == list(range(2, 26))
    assert store.snapshot()["data"]["count"] == 24


def test_snapshot_interleaved_with_atomic_writes_is_always_coherent(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")

    def writer() -> None:
        for _ in range(50):
            store.mutate(lease, _increment)

    def reader() -> None:
        for _ in range(100):
            state = store.snapshot()
            assert state["revision"] == state["data"].get("count", 0) + 1

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(writer), *(executor.submit(reader) for _ in range(3))]
        for future in futures:
            future.result()
    assert store.snapshot()["data"]["count"] == 50


def test_competing_processes_elect_exactly_one_holder(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = [context.Process(target=_compete, args=(str(tmp_path), str(i), start, results))
                 for i in range(4)]
    for process in processes:
        process.start()
    start.set()
    _join(processes)
    outcomes = [results.get(timeout=5) for _ in processes]
    winners = [owner for owner, fence in outcomes if fence == 1]
    assert len(winners) == 1
    state = OrchestrationStore(tmp_path, clock=Clock()).snapshot()
    assert state["lease"]["owner"] == winners[0]
    assert state["revision"] == 1
    results.close()


def test_concurrent_trusted_processes_with_same_lease_do_not_lose_updates(tmp_path: Path) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [context.Process(target=_process_mutate, args=(str(tmp_path), lease, start))
                 for _ in range(4)]
    for process in processes:
        process.start()
    start.set()
    _join(processes)
    assert store.snapshot()["revision"] == 49
    assert store.snapshot()["data"]["count"] == 48


def test_crash_before_replace_preserves_state_and_releases_lock(tmp_path: Path) -> None:
    clock = Clock()
    store = OrchestrationStore(tmp_path, clock=clock)
    lease = store.acquire("old", 1)
    before = _state_path(tmp_path).read_bytes()
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_before_replace, args=(str(tmp_path), lease))
    try:
        process.start()
        process.join(timeout=15)
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert _state_path(tmp_path).read_bytes() == before
    abandoned = list(tmp_path.glob(".state-*.tmp"))
    assert len(abandoned) == 1
    assert json.loads(abandoned[0].read_bytes())["data"] == {"count": 1}
    clock.value = 101
    new = OrchestrationStore(tmp_path, clock=clock).acquire("new")
    assert new.fence == 2
    assert store.mutate(new, _increment)["data"] == {"count": 1}
    assert abandoned[0].exists()


@pytest.mark.parametrize("change", [
    {"owner": "other"}, {"fence": 2}, {"fence": 0}, {"fence": True},
    {"expires_at": 131.0}, {"expires_at": float("nan")},
])
def test_forged_or_invalid_lease_cannot_write(tmp_path: Path, change: dict[str, Any]) -> None:
    store = OrchestrationStore(tmp_path, clock=Clock())
    lease = store.acquire("holder")
    before = _state_path(tmp_path).read_bytes()
    with pytest.raises(OrchestrationStoreError):
        store.mutate(replace(lease, **change), _increment)
    assert _state_path(tmp_path).read_bytes() == before
