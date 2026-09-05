"""使用可重放 JSONL 保存 Runtime 事件，不改变 Requirement 或 Session 事实。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..workspace import WorkspaceError, _file_lock
from .contracts import AgentEvent

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class RuntimeEventStoreError(WorkspaceError):
    """事件内容、持久文件或游标不满足追加日志契约。"""


class RuntimeEventStore:
    """按 run_id 隔离追加日志；序号从 1 开始，after 为排他游标。

    每次操作均复用工作区的线程/进程文件锁。append 在返回前 flush 和
    fsync，因此读者无需等待 Agent 进程退出。幂等重试必须保留原事件的
    全部内容（包括 timestamp 和未知字段），仅 sequence 由本存储分配。
    JSONL 是唯一事件事实来源；没有需要在崩溃后同步的旁路索引。
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def append(self, event: AgentEvent) -> AgentEvent:
        """返回带持久 sequence 的事件；同一 ID 的不同内容拒绝写入。"""

        # 先复制并验证 JSON 数据，避免调用方在写入后修改 payload 的引用。
        incoming = _decode_event(_encode(event.to_dict()))
        log_path, lock_path = self._paths(incoming.run_id)
        with _file_lock(lock_path):
            self._check_path(log_path)
            events = self._read_locked(log_path, incoming.run_id)
            content = _identity(incoming)
            for previous in events:
                if previous.event_id == incoming.event_id:
                    if _identity(previous) != content:
                        raise RuntimeEventStoreError(
                            f"事件 ID 已存在但内容不同：{incoming.event_id}"
                        )
                    return previous
            document = incoming.to_dict()
            document["sequence"] = len(events) + 1
            assigned = _decode_event(_encode(document))
            with log_path.open("ab") as stream:
                stream.write(_encode(assigned.to_dict()) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            return assigned

    def replay(
        self, run_id: str, *, after: int = 0, limit: int = 1000
    ) -> tuple[AgentEvent, ...]:
        """有序返回 sequence > after 的事件；未知 run 返回空元组。

        after 必须是非负整数；limit 必须是正整数。布尔值不是合法游标。
        即使请求很短的分页，也检查整个日志，避免把损坏前缀当作完整事实。
        """

        if type(after) is not int or after < 0:
            raise RuntimeEventStoreError("事件 after 游标必须是非负整数")
        if type(limit) is not int or limit <= 0:
            raise RuntimeEventStoreError("事件 limit 必须是正整数")
        log_path, lock_path = self._paths(run_id)
        with _file_lock(lock_path):
            self._check_path(log_path)
            events = self._read_locked(log_path, run_id)
            return tuple(events[after : after + limit])

    def _paths(self, run_id: str) -> tuple[Path, Path]:
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise RuntimeEventStoreError("run_id 必须是 1 至 128 位安全标识符，不能包含路径")
        if self.root.resolve() != self.root:
            raise RuntimeEventStoreError("事件根目录已被重定向")
        self.root.mkdir(parents=True, exist_ok=True)
        # 使用完整摘要避免 Windows 大小写折叠和保留设备名，保留原 run_id 在每条事件中。
        shard = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        log_path, lock_path = self.root / f"{shard}.jsonl", self.root / f"{shard}.lock"
        self._check_path(log_path)
        self._check_path(lock_path)
        return log_path, lock_path

    def _check_path(self, path: Path) -> None:
        if path.is_symlink() or path.resolve().parent != self.root:
            raise RuntimeEventStoreError(f"事件文件不能使用重定向路径：{path.name}")
        try:
            info = path.stat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeEventStoreError(f"事件文件必须是独立的普通文件：{path.name}")

    def _read_locked(self, path: Path, run_id: str) -> list[AgentEvent]:
        if not path.exists():
            return []
        raw = path.read_bytes()
        complete, separator, tail = raw.rpartition(b"\n")
        lines = complete.split(b"\n") if separator else []
        events: list[AgentEvent] = []
        identifiers: set[str] = set()
        for index, line in enumerate(lines, start=1):
            try:
                event = _decode_event(line)
                _validate_record(event, run_id, index, identifiers)
            except RuntimeEventStoreError as exc:
                raise RuntimeEventStoreError(
                    f"完整事件行损坏，拒绝自动截断：{path.name}:{index}：{exc}"
                ) from exc
            events.append(event)
            identifiers.add(event.event_id)
        if not tail:
            return events

        # 仅没有换行提交标记的尾段可恢复；完整行和完整但无效的事件均 fail closed。
        try:
            document = _decode_json(tail)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._backup_tail(path, tail)
            with path.open("r+b") as stream:
                stream.truncate(len(raw) - len(tail))
                stream.flush()
                os.fsync(stream.fileno())
            return events

        event = _event_from_document(document)
        _validate_record(event, run_id, len(events) + 1, identifiers)
        # JSON 已完整、只缺换行时保留事件本身，不将其误判为丢弃候选。
        with path.open("ab") as stream:
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        events.append(event)
        return events

    def _backup_tail(self, path: Path, tail: bytes) -> None:
        backup = path.with_name(f"{path.stem}.tail-{uuid4().hex}.bin")
        self._check_path(backup)
        with backup.open("xb") as stream:
            stream.write(tail)
            stream.flush()
            os.fsync(stream.fileno())
        # POSIX 还需落盘目录项，再允许截断；Windows 的 fsync 已提交文件数据。
        if os.name != "nt":
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _encode(document: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeEventStoreError(f"事件必须是合法 JSON：{exc}") from exc


def _reject_constant(value: str) -> Any:
    raise RuntimeEventStoreError(f"事件不能包含非标准 JSON 数字：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeEventStoreError(f"事件 JSON 数字超出有限浮点范围：{value}")
    return number


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeEventStoreError(f"事件 JSON 字段重复：{key}")
        result[key] = value
    return result


def _decode_json(line: bytes) -> Any:
    return json.loads(
        line.decode("utf-8"), object_pairs_hook=_unique_fields,
        parse_constant=_reject_constant, parse_float=_finite_float,
    )


def _event_from_document(document: Any) -> AgentEvent:
    if not isinstance(document, dict):
        raise RuntimeEventStoreError("事件 JSON 必须是对象")
    try:
        return AgentEvent.from_dict(document)
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeEventStoreError(f"事件字段不合法：{exc}") from exc


def _decode_event(line: bytes) -> AgentEvent:
    try:
        return _event_from_document(_decode_json(line))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeEventStoreError(f"事件 JSON 损坏：{exc}") from exc


def _validate_record(
    event: AgentEvent, run_id: str, sequence: int, identifiers: set[str]
) -> None:
    if event.run_id != run_id:
        raise RuntimeEventStoreError("事件 run_id 与日志分片不一致")
    if type(event.sequence) is not int or event.sequence != sequence:
        raise RuntimeEventStoreError("事件 sequence 必须从 1 开始连续递增")
    if event.event_id in identifiers:
        raise RuntimeEventStoreError(f"持久日志包含重复 event_id：{event.event_id}")


def _identity(event: AgentEvent) -> bytes:
    document = event.to_dict()
    document.pop("sequence", None)
    return _encode(document)
