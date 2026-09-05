"""可信本地控制面的单写者租约与原子状态，不承担恶意 Worker 的权限隔离。"""

from __future__ import annotations

import copy
import json
import math
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..workspace import WorkspaceError, _file_lock


class OrchestrationStoreError(WorkspaceError):
    """控制面状态、时钟或租约不满足一致性契约。"""


@dataclass(frozen=True)
class SupervisorLease:
    """每次续租返回新凭据；旧 expires_at 对象不能继续写入。"""

    owner: str
    fence: int
    expires_at: float


class OrchestrationStore:
    """持久化人类可读 JSON；锁只协调可信 Supervisor，不构成 OS 安全边界。

    snapshot 是纯读，返回过期租约原貌，由 acquire 确定性接管。写入在锁内
    先校验租约与版本，变更后再次校验，临时文件 fsync 后原子替换。
    last_observed_at 是最近提交的决策时间；当前实例也记住纯读观察的时钟上界。
    """

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(os.path.abspath(root))
        self._clock = clock
        self._clock_guard = threading.Lock()
        self._last_seen = 0.0

    def snapshot(self) -> dict[str, Any]:
        """原子读取并深复制；不存在的状态返回初始 envelope，不创建目录。"""

        with self._io_errors():
            self._check_paths()
            if not (self.root / "state.json").exists():
                state = _initial_state()
                self._now(state)
                return state
            # Windows 普通读句柄会阻止 replace。只复用已有且已初始化的锁，
            # 不为查询创建目录或文件；正常 acquire 总在首次 state 之前建立锁。
            lock = self.root / "state.lock"
            if not lock.exists() or lock.stat().st_size == 0:
                raise OrchestrationStoreError("Supervisor 持久状态缺少已初始化的锁文件")
            with _file_lock(lock):
                state = self._read()
                self._now(state)
                return copy.deepcopy(state)

    def acquire(self, owner: str, ttl_seconds: float = 30) -> SupervisorLease:
        """仅在没有有效 holder 时取得更大的 fence；同 owner 也不能重复取得。"""

        _validate_owner(owner)
        ttl = _positive_ttl(ttl_seconds)
        with self._locked():
            state = self._read()
            now = self._now(state)
            current = state["lease"]
            if current is not None and current["expires_at"] > now:
                raise OrchestrationStoreError("Supervisor 租约仍由有效 holder 持有")
            updated = copy.deepcopy(state)
            updated["fence"] += 1
            lease = SupervisorLease(owner, updated["fence"], _expiry(now, ttl))
            lease_document = copy.deepcopy(current) if current is not None else {}
            lease_document.update(owner=owner, fence=lease.fence, expires_at=lease.expires_at)
            updated["lease"] = lease_document
            self._commit(state, updated, now, lambda stamp: _not_expired(lease, stamp))
            return lease

    def renew(self, lease: SupervisorLease, ttl_seconds: float = 30) -> SupervisorLease:
        """不能复活过期租约；成功后必须使用返回的新 lease 对象。"""

        _validate_lease(lease)
        ttl = _positive_ttl(ttl_seconds)
        with self._locked():
            state = self._read()
            now = self._now(state)
            self._require_lease(state, lease, now)
            updated = copy.deepcopy(state)
            renewed = SupervisorLease(lease.owner, lease.fence, _expiry(now, ttl))
            # 相同的 clock/ttl 可得到相同凭据，但仍有一次可观察的 revision 递增。
            updated["lease"]["expires_at"] = renewed.expires_at

            def validate(stamp: float) -> None:
                self._require_lease(state, lease, stamp)
                _not_expired(renewed, stamp)

            self._commit(state, updated, now, validate)
            return renewed

    def release(self, lease: SupervisorLease) -> None:
        """只有当前且未过期的 lease 可以释放；旧 holder 不得清除后来者。"""

        _validate_lease(lease)
        with self._locked():
            state = self._read()
            now = self._now(state)
            self._require_lease(state, lease, now)
            updated = copy.deepcopy(state)
            updated["lease"] = None
            self._commit(
                state, updated, now, lambda stamp: self._require_lease(state, lease, stamp)
            )

    def mutate(
        self,
        lease: SupervisorLease,
        change: Callable[[dict[str, Any]], None],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """change 只操作 data；异常、版本冲突和跨过租约到期均不提交该变更。"""

        _validate_lease(lease)
        if expected_revision is not None:
            _nonnegative_integer(expected_revision, "expected_revision")
        with self._locked():
            state = self._read()
            self._require_lease(state, lease, self._now(state))
            if expected_revision is not None and state["revision"] != expected_revision:
                raise OrchestrationStoreError("Supervisor 状态 revision 冲突")
            updated = copy.deepcopy(state)
            change(updated["data"])
            now = self._now(state)
            self._require_lease(state, lease, now)
            return self._commit(
                state, updated, now, lambda stamp: self._require_lease(state, lease, stamp)
            )

    @contextmanager
    def _io_errors(self) -> Iterator[None]:
        try:
            yield
        except OrchestrationStoreError:
            raise
        except WorkspaceError as exc:
            raise OrchestrationStoreError(str(exc)) from exc
        except OSError as exc:
            raise OrchestrationStoreError(f"无法访问 Supervisor 状态：{exc}") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._io_errors():
            self._check_paths()
            self.root.mkdir(parents=True, exist_ok=True)
            self._check_paths()
            with _file_lock(self.root / "state.lock"):
                self._check_paths()
                yield

    def _check_paths(self) -> None:
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise OrchestrationStoreError("Supervisor 根目录不能使用重定向路径")
        if self.root.exists() and not self.root.is_dir():
            raise OrchestrationStoreError("Supervisor 根路径必须是目录")
        self._check_file(self.root / "state.json")
        self._check_file(self.root / "state.lock")

    def _check_file(self, path: Path) -> None:
        if path.is_symlink() or path.resolve().parent != self.root:
            raise OrchestrationStoreError(f"Supervisor 文件不能使用重定向路径：{path.name}")
        try:
            info = path.stat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OrchestrationStoreError(f"Supervisor 文件必须是独立普通文件：{path.name}")

    def _read(self) -> dict[str, Any]:
        self._check_paths()
        try:
            raw = (self.root / "state.json").read_bytes()
        except FileNotFoundError:
            return _initial_state()
        try:
            document = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_fields,
                parse_constant=_reject_constant, parse_float=_finite_float,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise OrchestrationStoreError("Supervisor JSON 损坏，保留原文件") from exc
        return _validate_state(document)

    def _now(self, state: dict[str, Any]) -> float:
        with self._clock_guard:
            now = _finite_nonnegative(self._clock(), "clock")
            if now < max(state["last_observed_at"], self._last_seen):
                raise OrchestrationStoreError("Supervisor 时钟倒退，拒绝继续写入或接管")
            self._last_seen = now
            return now

    @staticmethod
    def _require_lease(state: dict[str, Any], lease: SupervisorLease, now: float) -> None:
        current = state["lease"]
        if current is None or (
            current["owner"], current["fence"], current["expires_at"]
        ) != (lease.owner, lease.fence, lease.expires_at):
            raise OrchestrationStoreError("Supervisor lease 已失效或 fence 不匹配")
        _not_expired(lease, now)

    def _commit(
        self,
        previous: dict[str, Any],
        updated: dict[str, Any],
        now: float,
        validate_lease: Callable[[float], None],
    ) -> dict[str, Any]:
        # change 可能保留 data 的引用，提交后不再与调用方共享可变对象。
        updated = copy.deepcopy(updated)
        updated["revision"] = previous["revision"] + 1
        updated["last_observed_at"] = now
        _validate_state(updated)
        raw = _encode(updated)
        temporary = self.root / f".state-{uuid4().hex}.tmp"
        self._check_file(temporary)
        created = False
        try:
            with temporary.open("xb") as stream:
                created = True
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_paths()
            # 文件锁支持重入；change 意外嵌套提交也不得让外层覆盖新状态。
            if self._read() != previous:
                raise OrchestrationStoreError("Supervisor 状态在变更期间发生 revision 冲突")
            validate_lease(self._now(updated))
            os.replace(temporary, self.root / "state.json")
            if os.name != "nt":
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            # 只移除本次创建的精确临时路径；崩溃留下的其他文件保持原状。
            if created and temporary.exists():
                self._check_file(temporary)
                temporary.unlink()
        return copy.deepcopy(updated)


def _initial_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": 0,
        "fence": 0,
        "lease": None,
        "last_observed_at": 0.0,
        "data": {},
    }


def _validate_owner(owner: str) -> None:
    if not isinstance(owner, str) or not owner.strip():
        raise OrchestrationStoreError("Supervisor owner 必须是非空字符串")


def _finite_nonnegative(value: Any, field: str) -> float:
    if type(value) not in (int, float):
        raise OrchestrationStoreError(f"Supervisor {field} 必须是有限非负数字")
    try:
        number = float(value)
    except OverflowError as exc:
        raise OrchestrationStoreError(f"Supervisor {field} 超出有限数字范围") from exc
    if not math.isfinite(number) or number < 0:
        raise OrchestrationStoreError(f"Supervisor {field} 必须是有限非负数字")
    return number


def _positive_ttl(value: float) -> float:
    number = _finite_nonnegative(value, "ttl_seconds")
    if number == 0:
        raise OrchestrationStoreError("Supervisor ttl_seconds 必须大于 0")
    return number


def _expiry(now: float, ttl: float) -> float:
    result = _finite_nonnegative(now + ttl, "expires_at")
    if result <= now:
        raise OrchestrationStoreError("Supervisor ttl_seconds 不能小于当前时钟精度")
    return result


def _nonnegative_integer(value: Any, field: str) -> None:
    if type(value) is not int or value < 0:
        raise OrchestrationStoreError(f"Supervisor {field} 必须是非负整数")


def _validate_lease(lease: SupervisorLease) -> None:
    if not isinstance(lease, SupervisorLease):
        raise OrchestrationStoreError("必须提供 SupervisorLease 对象")
    _validate_owner(lease.owner)
    _nonnegative_integer(lease.fence, "lease.fence")
    if lease.fence == 0:
        raise OrchestrationStoreError("Supervisor lease.fence 必须大于 0")
    _finite_nonnegative(lease.expires_at, "lease.expires_at")


def _not_expired(lease: SupervisorLease, now: float) -> None:
    if lease.expires_at <= now:
        raise OrchestrationStoreError("Supervisor lease 已过期")


def _validate_state(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise OrchestrationStoreError("Supervisor 状态必须是 JSON 对象")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise OrchestrationStoreError("不支持此 Supervisor schema_version，保留原文件")
    for field in ("revision", "fence"):
        _nonnegative_integer(document.get(field), field)
    if document["revision"] < document["fence"]:
        raise OrchestrationStoreError("Supervisor revision 不能小于 fence")
    _finite_nonnegative(document.get("last_observed_at"), "last_observed_at")
    if not isinstance(document.get("data"), dict) or "lease" not in document:
        raise OrchestrationStoreError("Supervisor 状态缺少 data 对象或 lease 字段")
    current = document["lease"]
    if current is not None:
        if not isinstance(current, dict):
            raise OrchestrationStoreError("Supervisor lease 必须是对象或 null")
        if not {"owner", "fence", "expires_at"}.issubset(current):
            raise OrchestrationStoreError("Supervisor lease 缺少必要字段")
        lease = SupervisorLease(
            current["owner"], current["fence"], current["expires_at"]
        )
        _validate_lease(lease)
        if lease.fence != document["fence"]:
            raise OrchestrationStoreError("Supervisor lease.fence 与持久 fence 不一致")
    return document


def _encode(document: dict[str, Any]) -> bytes:
    try:
        _json_types(document)
        return (json.dumps(
            document, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise OrchestrationStoreError(f"Supervisor 状态必须是合法 JSON：{exc}") from exc


def _json_types(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise OrchestrationStoreError("Supervisor JSON 对象字段必须是字符串")
            _json_types(item)
    elif isinstance(value, list):
        for item in value:
            _json_types(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise OrchestrationStoreError("Supervisor 状态包含非 JSON 数据类型")


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise OrchestrationStoreError(f"Supervisor JSON 字段重复：{key}")
        document[key] = value
    return document


def _reject_constant(value: str) -> Any:
    raise OrchestrationStoreError(f"Supervisor JSON 不能包含非标准数字：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise OrchestrationStoreError("Supervisor JSON 数字超出有限浮点范围")
    return number
