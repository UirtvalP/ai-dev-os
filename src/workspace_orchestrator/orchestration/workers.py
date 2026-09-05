"""受信 Runtime 执行端口：先持久化尝试，再启动外层隔离的真实进程。

账本和事件写入发生在控制面，Worker 只拿到文本管道。进程退出、Agent 自报
完成、OS 隔离与候选 Git 身份是不同事实，任何一项未知都不能升级成已验收。
"""

from __future__ import annotations

import copy
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ..agent_runtime.contracts import AgentEvent, AgentRunRequest, EventSink, RuntimeSessionRef
from ..agent_runtime.events import RuntimeEventStore
from ..agent_runtime.ports import AgentRuntimePort
from ..agent_runtime.stdio import JsonRpcStdioClient, ProcessFactory, RuntimeProcess
from .contracts import (
    ModelRoute,
    PolicyError,
    TaskSpec,
    WorkerIsolation,
    WorkerObservation,
    fingerprint,
)
from .isolation import IsolationCapability, WorkerIsolationSpec
from .store import OrchestrationStore

_TERMINAL = frozenset({"candidate_complete", "blocked", "failed"})


class IsolationLauncher(Protocol):
    """仅控制面安装的实现可提供 launcher；运行策略不是仓库可写配置。"""

    def probe(self, spec: WorkerIsolationSpec) -> IsolationCapability: ...
    def launch(
        self, spec: WorkerIsolationSpec, command: Sequence[str], *,
        environ: Mapping[str, str] | None = None,
    ) -> RuntimeProcess: ...


class IsolatedRuntimeFactory(Protocol):
    def __call__(
        self, runtime_id: str, /, *, event_sink: EventSink,
        client_factory: Callable[..., JsonRpcStdioClient],
    ) -> AgentRuntimePort: ...


@dataclass
class _ActiveAttempt:
    fence: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    guard: threading.RLock = field(default_factory=threading.RLock)
    runtime: AgentRuntimePort | None = None
    session: RuntimeSessionRef | None = None
    turn_id: str | None = None
    thread: threading.Thread | None = None
    transport: JsonRpcStdioClient | None = None


class RuntimeWorkerPort:
    """每个控制器拥有本地进程句柄，跨控制器只共享不可伪造的控制面账本。

    重启后没有句柄的非终态尝试返回 unknown，不以 PID 存活探测、租约过期或
    空事件日志猜测可重放。调用者应保留槽位并升级人工处理。
    candidate_reader 必须由可信 Git 边界提供，不能在这里直接运行 Worker
    可改写的 Git 配置/过滤器；缺失时停在 blocked，后续 Git Provider 再接入。
    """

    def __init__(
        self, root: Path, *, requirement_id: str, runtime_factory: IsolatedRuntimeFactory,
        launcher: IsolationLauncher, protected_roots: tuple[Path, ...],
        readonly_tools: tuple[Path, ...] = (), allow_network: bool = False,
        candidate_reader: Callable[[TaskSpec], tuple[str, str]] | None = None,
        timeout_seconds: float = 7200,
    ) -> None:
        if (not requirement_id.strip() or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("Requirement ID 不能为空，Worker 超时必须为正数")
        self.root = root.absolute()
        self.requirement_id = requirement_id
        self.runtime_factory, self.launcher = runtime_factory, launcher
        # 正在运行的控制面源码也必须在授权写目录外，禁止用可写 editable 安装自证隔离。
        self.protected_roots = tuple(dict.fromkeys((
            *protected_roots, self.root, Path(__file__).resolve().parents[1],
        )))
        self.readonly_tools, self.allow_network = readonly_tools, allow_network
        self.candidate_reader, self.timeout_seconds = candidate_reader, timeout_seconds
        self.store = OrchestrationStore(self.root / "ledger")
        self.events = RuntimeEventStore(self.root / "events")
        self._owner = f"runtime-port-{uuid4()}"
        self._guard = threading.RLock()
        self._active: dict[str, _ActiveAttempt] = {}

    def _spec(self, task: TaskSpec, attempt_id: str, fence: int) -> WorkerIsolationSpec:
        task.validate()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", attempt_id):
            raise PolicyError("invalid_attempt", "尝试 ID 必须是安全、有限长度的标识符")
        if not task.worktree:
            raise PolicyError("worktree_missing", "Worker 必须有明确的独立 Task worktree")
        return WorkerIsolationSpec(
            Path(task.worktree), self.protected_roots, self.readonly_tools,
            attempt_id, fence, self.allow_network,
        )

    def isolation(self, task: TaskSpec) -> WorkerIsolation:
        capability = self.launcher.probe(self._spec(task, "isolation-preflight", 1))
        return WorkerIsolation(
            capability.backend, capability.supported,
            (str(Path(task.worktree or "").absolute()),),
            tuple(str(path) for path in self.protected_roots), capability.reason,
        )

    def _mutate(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._guard:
            lease = self.store.acquire(self._owner, ttl_seconds=30)
            try:
                result: dict[str, Any] = self.store.mutate(lease, change)["data"]
                return result
            finally:
                self.store.release(lease)

    def _read(self, attempt_id: str) -> dict[str, Any] | None:
        value: dict[str, Any] | None = self.store.snapshot()["data"].get("attempts", {}).get(attempt_id)
        if value is not None and not isinstance(value, dict):
            raise PolicyError("invalid_ledger", "Worker 尝试账本记录必须是对象")
        return copy.deepcopy(value)

    @staticmethod
    def _fence(data: dict[str, Any], fence: int) -> None:
        if type(fence) is not int or fence < 1:
            raise PolicyError("invalid_fence", "Worker fence 必须为正整数")
        if fence < data.get("highest_fence", 0):
            raise PolicyError("stale_fence", "拒绝旧 Supervisor epoch 的 Worker 操作")
        data["highest_fence"] = fence

    def dispatch(
        self, attempt_id: str, fence: int, task: TaskSpec, route: ModelRoute,
    ) -> WorkerObservation:
        task.validate()
        route.validate()
        # 标识符和权限探测在账本或外部副作用之前完成。
        spec = self._spec(task, attempt_id, fence)
        capability = self.launcher.probe(spec)
        if not capability.supported:
            raise PolicyError("isolation_failure", capability.reason)
        identity = fingerprint({"task": task.to_dict(), "route": route.to_dict()})
        with self._guard:
            created = False

            def register(data: dict[str, Any]) -> None:
                nonlocal created
                self._fence(data, fence)
                attempts = data.setdefault("attempts", {})
                previous = attempts.get(attempt_id)
                if previous is not None:
                    if previous["fingerprint"] != identity or previous["fence"] != fence:
                        raise PolicyError("attempt_conflict", "尝试 ID 已绑定不同输入或 epoch")
                    return
                # 即使上层误用接口，也不允许两个未确认终止的尝试争用同一写目录。
                root = Path(task.worktree or "").resolve()
                for value in attempts.values():
                    if value["observation"]["state"] not in _TERMINAL:
                        other = Path(value["task"]["worktree"]).resolve()
                        if root == other or root in other.parents or other in root.parents:
                            raise PolicyError("worktree_busy", "Task worktree 仍被未终止尝试占用")
                attempts[attempt_id] = {
                    "fingerprint": identity, "fence": fence, "task": task.to_dict(),
                    "route": route.to_dict(), "isolation": {
                        "backend": capability.backend, "evidence": capability.evidence,
                    }, "observation": WorkerObservation(attempt_id, fence, "running").to_dict(),
                }
                created = True

            self._mutate(register)
            if created:
                active = _ActiveAttempt(fence)
                self._active[attempt_id] = active
                active.thread = threading.Thread(
                    target=self._execute, args=(attempt_id, task, route, spec, active),
                    name=f"worker-{attempt_id}", daemon=True,
                )
                try:
                    active.thread.start()
                except BaseException:
                    self._save(WorkerObservation(
                        attempt_id, fence, "failed", error_class="launch_failed",
                        summary="控制面线程未能启动，未创建 Worker 子进程",
                    ))
                    active.finished.set()
                    raise
            return self._observation(attempt_id)

    def _save(self, observation: WorkerObservation) -> None:
        observation.validate()

        def update(data: dict[str, Any]) -> None:
            existing = data.get("attempts", {}).get(observation.attempt_id)
            if existing is None or existing["fence"] != observation.fence:
                raise PolicyError("attempt_conflict", "Worker 结果不属于已登记尝试")
            # 允许原 launcher 在后续 epoch 中记录真正停止的旧进程；保留原 fence。
            # 新 Supervisor 不能把此记录作为自身尝试的成功响应。
            existing["observation"] = observation.to_dict()

        self._mutate(update)

    def _observation(self, attempt_id: str) -> WorkerObservation:
        record = self._read(attempt_id)
        if record is None:
            raise PolicyError("attempt_missing", "尝试不在可信执行账本中")
        observation = WorkerObservation.from_dict(record["observation"])
        if observation.state not in _TERMINAL and attempt_id not in self._active:
            return WorkerObservation(
                attempt_id, observation.fence, "unknown", session_id=observation.session_id,
                error_class="ambiguous", summary="控制器重启，缺少此尝试的进程句柄；不得重放",
            )
        return observation

    def _execute(
        self, attempt_id: str, task: TaskSpec, route: ModelRoute,
        spec: WorkerIsolationSpec, active: _ActiveAttempt,
    ) -> None:
        runtime: AgentRuntimePort | None = None
        observation = WorkerObservation(attempt_id, active.fence, "unknown")

        def process_factory(
            command: Sequence[str], *, cwd: Path | None, env: Mapping[str, str] | None,
        ) -> RuntimeProcess:
            if cwd is None or cwd.resolve() != spec.task_root.resolve():
                raise PolicyError("scope_mismatch", "Runtime 试图在 Task 域外启动")
            if active.cancelled.is_set():
                raise OSError("Worker 已取消，拒绝创建进程")
            process = self.launcher.launch(spec, command, environ=env)
            try:
                evidence = getattr(process, "isolation_evidence", None)
                if evidence is not None:
                    def audit(data: dict[str, Any]) -> None:
                        record = data["attempts"][attempt_id]
                        if record["fence"] != active.fence:
                            raise PolicyError("stale_fence", "进程启动时尝试 fence 已变化")
                        record["launch_evidence"] = copy.deepcopy(evidence)

                    # Windows 此时仍挂起；审计失败必须先回收，不把失联进程交给外部。
                    self._mutate(audit)
                return process
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                closer = getattr(process, "close", None)
                if callable(closer):
                    closer()
                raise

        factory: ProcessFactory = process_factory

        def client_factory(*args: Any, **kwargs: Any) -> JsonRpcStdioClient:
            with active.guard:
                if active.transport is not None:
                    raise PolicyError("duplicate_launch", "每个尝试只能创建一棵隔离进程树")
                if "process_factory" in kwargs:
                    raise PolicyError("isolation_failure", "Runtime 不能覆盖可信隔离 Launcher")
                transport = JsonRpcStdioClient(*args, **kwargs, process_factory=factory)
                active.transport = transport
                return transport

        def sink(event: AgentEvent) -> None:
            if event.run_id != attempt_id or event.runtime_id != route.runtime_id:
                raise PolicyError("scope_mismatch", "事件不属于当前 Worker Runtime/attempt")
            # 原始输出仅是事件数据；不能调用 Gate、更新 node 或发起投影。
            self.events.append(event)

        try:
            runtime = self.runtime_factory(
                route.runtime_id, event_sink=sink, client_factory=client_factory,
            )
            with active.guard:
                active.runtime = runtime
            if active.cancelled.is_set():
                raise PolicyError("cancelled", "Worker 在启动前被取消")
            started = runtime.start(AgentRunRequest(
                attempt_id, spec.task_root, task.prompt, sandbox=route.sandbox, model=route.model,
                reasoning_effort=route.effort, requirement_id=self.requirement_id,
                task_id=task.task_id, timeout_seconds=self.timeout_seconds,
            ))
            with active.guard:
                active.session, active.turn_id = started.session, started.turn_id
            if not started.ok or started.session is None or started.turn_id is None:
                error = started.error.code if started.error else "protocol_error"
                observation = WorkerObservation(
                    attempt_id, active.fence, "failed", error_class=error,
                    session_id=started.session.session_id if started.session else None,
                    summary=started.error.message if started.error else "Runtime 未确认真实轮次",
                )
            else:
                if (started.session.run_id != attempt_id
                        or started.session.runtime_id != route.runtime_id):
                    raise PolicyError("scope_mismatch", "Runtime 返回了其他尝试的会话")
                if active.transport is None or active.transport.pid is None:
                    raise PolicyError("isolation_failure", "Runtime 未通过可信 transport 启动进程")
                self._save(WorkerObservation(
                    attempt_id, active.fence, "running", session_id=started.session.session_id,
                ))
                result = runtime.wait(
                    started.session, started.turn_id, timeout_seconds=self.timeout_seconds,
                )
                if (result.run_id != attempt_id or result.session_id != started.session.session_id
                        or result.runtime_id != route.runtime_id):
                    raise PolicyError("scope_mismatch", "Runtime 返回了其他尝试的结果")
                observation = WorkerObservation(
                    attempt_id, active.fence, "blocked" if result.returncode == 0 else "failed",
                    session_id=result.session_id,
                    error_class="candidate_unverified" if result.returncode == 0 else (
                        result.error.code if result.error else "worker_failed"
                    ), summary=result.summary,
                )
        except Exception as exc:  # noqa: BLE001 -- 执行端口边界，仍须回收真实进程树。
            observation = WorkerObservation(
                attempt_id, active.fence, "failed", error_class=getattr(exc, "code", "worker_failed"),
                session_id=active.session.session_id if active.session else None, summary=str(exc),
            )
        finally:
            try:
                # Adapter close 之外独立关闭 transport，不能信任 Adapter 忘记收尾。
                try:
                    if runtime is not None:
                        runtime.close()
                finally:
                    if active.transport is not None:
                        active.transport.close()
                if active.cancelled.is_set():
                    observation = WorkerObservation(
                        attempt_id, active.fence, "failed", error_class="cancelled",
                        session_id=observation.session_id, summary="Worker 进程树已关闭",
                    )
                elif observation.error_class == "candidate_unverified" and self.candidate_reader:
                    sha, tree = self.candidate_reader(task)
                    observation = WorkerObservation(
                        attempt_id, active.fence, "candidate_complete",
                        session_id=observation.session_id, candidate_sha=sha, candidate_tree=tree,
                        summary=observation.summary,
                    )
            except Exception as exc:  # noqa: BLE001 -- 清理失败不得释放槽位或声称候选完成。
                observation = WorkerObservation(
                    attempt_id, active.fence, "unknown", error_class="cleanup_unconfirmed",
                    session_id=observation.session_id, summary=str(exc),
                )
            try:
                self._save(observation)
            finally:
                active.finished.set()

    def poll(self, attempt_id: str, fence: int) -> WorkerObservation:
        self._mutate(lambda data: self._fence(data, fence))
        return self._observation(attempt_id)

    def reconcile(self, attempt_id: str, fence: int) -> WorkerObservation:
        return self.poll(attempt_id, fence)

    def cancel(self, attempt_id: str, fence: int) -> WorkerObservation:
        record = self._read(attempt_id)
        if record is None or type(fence) is not int or fence != record["fence"]:
            raise PolicyError("stale_fence", "取消必须精确指向旧尝试自身的 fence")
        active = self._active.get(attempt_id)
        if active is None:
            return self._observation(attempt_id)
        active.cancelled.set()
        with active.guard:
            if active.transport is not None:
                active.transport.close()
        # 阻塞于创建/协议初始化时也不谎报已取消；下一次 poll 收到关闭后的结果。
        active.finished.wait(timeout=5)
        if active.finished.is_set():
            observation = self._observation(attempt_id)
            if observation.state in _TERMINAL and observation.error_class != "cancelled":
                self._save(WorkerObservation(
                    attempt_id, fence, "failed", error_class="cancelled",
                    session_id=observation.session_id, summary="已确认旧 Worker 进程树关闭并撤销候选",
                ))
        return self._observation(attempt_id)

    def close(self) -> None:
        for attempt_id, active in tuple(self._active.items()):
            if not active.finished.is_set():
                self.cancel(attempt_id, active.fence)
