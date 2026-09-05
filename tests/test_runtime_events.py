"""Runtime 事件日志的独立进程、游标和崩溃边界契约。"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from workspace_orchestrator.agent_runtime.contracts import AgentEvent
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore, RuntimeEventStoreError
from workspace_orchestrator.workspace import _file_lock

STAMP = "2026-09-05T12:00:00+00:00"


def _event(identifier: str, *, run_id: str = "run-1", **changes: Any) -> AgentEvent:
    event = AgentEvent(
        event_id=identifier,
        run_id=run_id,
        runtime_id="fake-runtime",
        kind="message.delta",
        payload={"text": "你好", "raw": {"future": [1, True, None]}},
        timestamp=STAMP,
    )
    return replace(event, **changes)


def _log(root: Path, run_id: str = "run-1") -> Path:
    return root / f"{hashlib.sha256(run_id.encode('utf-8')).hexdigest()}.jsonl"


def _raw_event(event: AgentEvent) -> bytes:
    return json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8")


def _process_append(root: str, index: int, start: Any) -> None:
    store = RuntimeEventStore(Path(root))
    if not start.wait(15):
        raise RuntimeError("等待并发测试开始超时")
    store.append(_event("shared"))
    for offset in range(12):
        store.append(_event(f"worker-{index}-{offset}"))


def _live_writer(root: str, ready: Any, finish: Any) -> None:
    store = RuntimeEventStore(Path(root))
    store.append(_event("before-exit"))
    ready.set()
    if not finish.wait(15):
        raise RuntimeError("等待读者确认超时")
    store.append(_event("after-read"))


def _crashing_writer(root: str) -> None:
    store = RuntimeEventStore(Path(root))
    log_path, lock_path = store._paths("run-1")
    with _file_lock(lock_path), log_path.open("ab") as stream:
        stream.write(b'{"event_id":"crashed", "payload":"\xe4\xbd')
        stream.flush()
        os.fsync(stream.fileno())
        os._exit(23)


def test_append_replay_and_exclusive_cursor(tmp_path: Path) -> None:
    store = RuntimeEventStore(tmp_path)
    assert store.replay("run-1") == ()
    first = _event("e1", sequence=987)
    assigned = store.append(first)
    assert assigned.sequence == 1
    assert first.sequence == 987
    for index in range(2, 5):
        assert store.append(_event(f"e{index}")).sequence == index

    assert [event.event_id for event in store.replay("run-1")] == ["e1", "e2", "e3", "e4"]
    assert [event.sequence for event in store.replay("run-1", after=1, limit=2)] == [2, 3]
    assert store.replay("run-1", after=4) == ()
    assert store.replay("run-1", after=1000) == ()
    assert _log(tmp_path).read_bytes().count(b"\n") == 4


@pytest.mark.parametrize("after", [-1, True, False, 1.1, "1", None])
def test_replay_rejects_invalid_cursor(tmp_path: Path, after: Any) -> None:
    with pytest.raises(RuntimeEventStoreError, match="after"):
        RuntimeEventStore(tmp_path).replay("run-1", after=after)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.1, "1", None])
def test_replay_rejects_invalid_limit(tmp_path: Path, limit: Any) -> None:
    with pytest.raises(RuntimeEventStoreError, match="limit"):
        RuntimeEventStore(tmp_path).replay("run-1", limit=limit)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "run_id",
    ["", ".", "..", "../outside", "../../outside", "a/b", "a\\b", "C:run", "/run", "a ",
     "a\n", "x" * 129, 1, None],
)
def test_run_id_cannot_select_paths(tmp_path: Path, run_id: Any) -> None:
    store = RuntimeEventStore(tmp_path)
    with pytest.raises(RuntimeEventStoreError):
        store.replay(run_id)
    with pytest.raises(RuntimeEventStoreError):
        store.append(_event("e1", run_id=run_id))
    assert list(tmp_path.iterdir()) == []


def test_run_partition_is_case_sensitive_even_on_windows(tmp_path: Path) -> None:
    store = RuntimeEventStore(tmp_path)
    for run_id in ("Run-1", "run-1", "NUL", "COM1", "a."):
        assert store.append(_event("shared-id", run_id=run_id)).sequence == 1
        assert store.replay(run_id)[0].run_id == run_id
    assert len(list(tmp_path.glob("*.jsonl"))) == 5


def test_idempotency_survives_reopen_and_preserves_unknown_fields(tmp_path: Path) -> None:
    original = _event("e1", extra={"future_schema": {"flags": ["x", "y"]}})
    assigned = RuntimeEventStore(tmp_path).append(original)
    before = _log(tmp_path).read_bytes()
    store = RuntimeEventStore(tmp_path)
    assert store.append(original) == assigned
    assert store.append(assigned) == assigned
    assert store.append(replace(original, sequence=1234)) == assigned
    assert store.replay("run-1") == (assigned,)
    assert _log(tmp_path).read_bytes() == before
    stored = json.loads(before)
    assert stored["future_schema"] == {"flags": ["x", "y"]}
    assert "extra" not in stored


@pytest.mark.parametrize(
    "change",
    [
        {"payload": {"text": "different"}},
        {"runtime_id": "other-runtime"},
        {"kind": "complete"},
        {"timestamp": "2026-09-05T12:00:01+00:00"},
        {"session_id": "different-session"},
        {"turn_id": "different-turn"},
        {"extra": {"future": True}},
    ],
)
def test_same_id_different_content_is_rejected(tmp_path: Path, change: dict[str, Any]) -> None:
    store = RuntimeEventStore(tmp_path)
    event = _event("e1")
    store.append(event)
    before = _log(tmp_path).read_bytes()
    with pytest.raises(RuntimeEventStoreError, match="ID 已存在但内容不同"):
        store.append(replace(event, **change))
    assert _log(tmp_path).read_bytes() == before


def test_events_are_detached_from_callers_mutable_payload(tmp_path: Path) -> None:
    store = RuntimeEventStore(tmp_path)
    event = _event("e1")
    assigned = store.append(event)
    event.payload["raw"]["future"].append("changed")
    assigned.payload["raw"]["future"].append("also changed")
    assert store.replay("run-1")[0].payload["raw"]["future"] == [1, True, None]


@pytest.mark.parametrize("payload", [{"x": float("nan")}, {"x": object()}, []])
def test_non_json_events_are_rejected_before_any_file_write(tmp_path: Path, payload: Any) -> None:
    with pytest.raises(RuntimeEventStoreError):
        RuntimeEventStore(tmp_path).append(_event("e1", payload=payload))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("tail", [b'{"event_id":', b"\xff\xfe", b'{"text":"\xe4\xbd'])
def test_unterminated_corrupt_tail_is_backed_up_before_repair(tmp_path: Path, tail: bytes) -> None:
    store = RuntimeEventStore(tmp_path)
    first = store.append(_event("e1"))
    prefix = _log(tmp_path).read_bytes()
    with _log(tmp_path).open("ab") as stream:
        stream.write(tail)
    assert store.replay("run-1") == (first,)
    assert _log(tmp_path).read_bytes() == prefix
    backups = list(tmp_path.glob("*.tail-*.bin"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == tail
    assert store.append(_event("e2")).sequence == 2
    assert len(list(tmp_path.glob("*.tail-*.bin"))) == 1


def test_entire_incomplete_first_record_is_preserved_in_backup(tmp_path: Path) -> None:
    tail = b'{"event_id":"unfinished"'
    _log(tmp_path).write_bytes(tail)
    store = RuntimeEventStore(tmp_path)
    assert store.append(_event("e1")).sequence == 1
    assert next(tmp_path.glob("*.tail-*.bin")).read_bytes() == tail
    assert len(store.replay("run-1")) == 1


def test_valid_final_record_without_newline_is_preserved(tmp_path: Path) -> None:
    first = replace(_event("e1", extra={"future": "retained"}), sequence=1)
    raw = _raw_event(first)
    _log(tmp_path).write_bytes(raw)
    store = RuntimeEventStore(tmp_path)
    assert store.replay("run-1") == (first,)
    assert _log(tmp_path).read_bytes() == raw + b"\n"
    assert list(tmp_path.glob("*.tail-*.bin")) == []
    assert store.append(_event("e2")).sequence == 2


@pytest.mark.parametrize("bad_line", [b"not-json\n", b"\xff\n", b"\n", b"[]\n"])
def test_complete_corrupt_line_fails_closed_without_touching_tail(
    tmp_path: Path, bad_line: bytes
) -> None:
    store = RuntimeEventStore(tmp_path)
    store.append(_event("e1"))
    raw = _log(tmp_path).read_bytes() + bad_line + b'{"unfinished":'
    _log(tmp_path).write_bytes(raw)
    with pytest.raises(RuntimeEventStoreError, match="完整事件行损坏"):
        store.replay("run-1", limit=1)
    with pytest.raises(RuntimeEventStoreError, match="完整事件行损坏"):
        store.append(_event("e3"))
    assert _log(tmp_path).read_bytes() == raw
    assert list(tmp_path.glob("*.tail-*.bin")) == []


@pytest.mark.parametrize(
    "change",
    [
        {"sequence": 3},
        {"sequence": True},
        {"run_id": "other-run"},
        {"event_id": "e1"},
        {"schema_version": 2},
        {"timestamp": "not-a-time"},
        {"payload": []},
    ],
)
@pytest.mark.parametrize("newline", [b"\n", b""])
def test_valid_json_with_invalid_event_is_never_discarded(
    tmp_path: Path, change: dict[str, Any], newline: bytes
) -> None:
    store = RuntimeEventStore(tmp_path)
    store.append(_event("e1"))
    document = _event("e2", sequence=2).to_dict()
    document.update(change)
    raw = _log(tmp_path).read_bytes() + json.dumps(document).encode("utf-8") + newline
    _log(tmp_path).write_bytes(raw)
    with pytest.raises(RuntimeEventStoreError):
        store.replay("run-1")
    assert _log(tmp_path).read_bytes() == raw
    assert list(tmp_path.glob("*.tail-*.bin")) == []


@pytest.mark.parametrize("bad_json", [
    b'{"a":1,"a":2}', b'{"future":NaN}',
    b'{"future":1e999}', b'{"future":-1e999}',
])
@pytest.mark.parametrize("newline", [b"\n", b""])
def test_ambiguous_json_is_never_repaired(tmp_path: Path, bad_json: bytes, newline: bytes) -> None:
    raw = bad_json + newline
    _log(tmp_path).write_bytes(raw)
    with pytest.raises(RuntimeEventStoreError):
        RuntimeEventStore(tmp_path).replay("run-1")
    assert _log(tmp_path).read_bytes() == raw
    assert list(tmp_path.glob("*.tail-*.bin")) == []


@pytest.mark.parametrize("newline", [b"\n", b""])
def test_nonfinite_future_payload_preserves_complete_record(tmp_path: Path, newline: bytes) -> None:
    document = _event("overflow", sequence=1, payload={"future_metric": "OVERFLOW"}).to_dict()
    raw = json.dumps(document).replace('"OVERFLOW"', "1e999").encode("utf-8") + newline
    _log(tmp_path).write_bytes(raw)
    with pytest.raises(RuntimeEventStoreError, match="有限浮点"):
        RuntimeEventStore(tmp_path).replay("run-1")
    assert _log(tmp_path).read_bytes() == raw
    assert list(tmp_path.glob("*.tail-*.bin")) == []


def test_backup_failure_cannot_truncate_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RuntimeEventStore(tmp_path)
    store.append(_event("e1"))
    raw = _log(tmp_path).read_bytes() + b"unfinished"
    _log(tmp_path).write_bytes(raw)

    def fail_backup(path: Path, tail: bytes) -> None:
        raise OSError("模拟备份失败")

    monkeypatch.setattr(store, "_backup_tail", fail_backup)
    with pytest.raises(OSError, match="模拟备份失败"):
        store.append(_event("e2"))
    assert _log(tmp_path).read_bytes() == raw


def test_interruption_after_backup_retries_without_losing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RuntimeEventStore(tmp_path)
    store.append(_event("e1"))
    tail = b"interrupted record"
    raw = _log(tmp_path).read_bytes() + tail
    _log(tmp_path).write_bytes(raw)
    backup_tail = store._backup_tail

    def backup_then_crash(path: Path, data: bytes) -> None:
        backup_tail(path, data)
        raise OSError("模拟备份后崩溃")

    monkeypatch.setattr(store, "_backup_tail", backup_then_crash)
    with pytest.raises(OSError, match="模拟备份后崩溃"):
        store.replay("run-1")
    assert _log(tmp_path).read_bytes() == raw
    assert next(tmp_path.glob("*.tail-*.bin")).read_bytes() == tail
    assert RuntimeEventStore(tmp_path).append(_event("e2")).sequence == 2
    assert all(path.read_bytes() == tail for path in tmp_path.glob("*.tail-*.bin"))


def test_concurrent_threads_assign_unique_sequences_and_deduplicate(tmp_path: Path) -> None:
    store = RuntimeEventStore(tmp_path)

    def append(index: int) -> int:
        store.append(_event("shared"))
        return store.append(_event(f"e{index}")).sequence

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(24)))
    assert sorted(sequences) == list(range(2, 26))
    assert [event.sequence for event in store.replay("run-1")] == list(range(1, 26))


def test_concurrent_processes_append_without_lost_or_duplicate_events(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(target=_process_append, args=(str(tmp_path), index, start))
        for index in range(4)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    events = RuntimeEventStore(tmp_path).replay("run-1")
    assert [event.sequence for event in events] == list(range(1, 50))
    assert len({event.event_id for event in events}) == 49


def test_reader_observes_event_before_writer_process_exits(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    process = context.Process(target=_live_writer, args=(str(tmp_path), ready, finish))
    try:
        process.start()
        assert ready.wait(timeout=15)
        assert process.is_alive()
        assert [event.event_id for event in RuntimeEventStore(tmp_path).replay("run-1")] == [
            "before-exit"
        ]
        finish.set()
        process.join(timeout=15)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert [event.sequence for event in RuntimeEventStore(tmp_path).replay("run-1")] == [1, 2]


def test_process_crash_releases_lock_and_preserves_partial_tail(tmp_path: Path) -> None:
    store = RuntimeEventStore(tmp_path)
    store.append(_event("before-crash"))
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crashing_writer, args=(str(tmp_path),))
    try:
        process.start()
        process.join(timeout=15)
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert store.append(_event("after-crash")).sequence == 2
    assert [event.event_id for event in store.replay("run-1")] == ["before-crash", "after-crash"]
    assert next(tmp_path.glob("*.tail-*.bin")).read_bytes() == (
        b'{"event_id":"crashed", "payload":"\xe4\xbd'
    )


def test_append_flushes_to_fsync_before_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[bytes] = []
    fsync = os.fsync

    def observe_fsync(descriptor: int) -> None:
        observed.append(_log(tmp_path).read_bytes())
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    assigned = RuntimeEventStore(tmp_path).append(_event("e1"))
    assert len(observed) == 1
    assert json.loads(observed[0]) == assigned.to_dict()


@pytest.mark.parametrize("suffix", ["jsonl", "lock"])
def test_directory_cannot_impersonate_event_file(tmp_path: Path, suffix: str) -> None:
    _log(tmp_path).with_suffix(f".{suffix}").mkdir()
    with pytest.raises(RuntimeEventStoreError, match="独立的普通文件"):
        RuntimeEventStore(tmp_path).append(_event("e1"))


@pytest.mark.parametrize("suffix", ["jsonl", "lock"])
def test_symlink_shard_cannot_escape_root(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "events"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    path = _log(root).with_suffix(f".{suffix}")
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("当前系统未授权创建符号链接")
    with pytest.raises(RuntimeEventStoreError, match="重定向路径"):
        RuntimeEventStore(root).replay("run-1")
    assert outside.read_bytes() == b"preserve"


@pytest.mark.parametrize("suffix", ["jsonl", "lock"])
def test_hardlinked_shard_cannot_modify_another_file(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "events"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    try:
        os.link(outside, _log(root).with_suffix(f".{suffix}"))
    except OSError:
        pytest.skip("当前文件系统不支持硬链接")
    with pytest.raises(RuntimeEventStoreError, match="独立的普通文件"):
        RuntimeEventStore(root).append(_event("e1"))
    assert outside.read_bytes() == b"preserve"
