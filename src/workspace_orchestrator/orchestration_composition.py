"""V2 控制面入口装配；具体 Runtime、Windows 隔离和外部投影不进入领域 Core。"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from .agent_runtime.codex import codex_command
from .automation.task_attach import configured_task_provider
from .composition import create_runtime, runtime_descriptors
from .orchestration.contracts import PlanningRequest
from .orchestration.isolation import WindowsAppContainerIsolation
from .orchestration.projection import TaskProjection, TaskProjectionPump
from .orchestration.store import OrchestrationStore
from .orchestration.supervisor import RequirementSupervisor
from .orchestration.workers import RuntimeWorkerPort
from .workspace import WorkspaceError, WorkspaceStore


def control_store(workspace: WorkspaceStore, requirement_id: str) -> OrchestrationStore:
    """先确认已有 Requirement，不由编排命令隐式创建或重新绑定 Session。"""
    workspace.load(requirement_id)
    return OrchestrationStore(workspace.path_for(requirement_id) / "orchestration" / "supervisor")


def configured_supervisor(
    workspace: WorkspaceStore, requirement_id: str, *, owner: str,
    max_workers: int = 1, allow_network: bool = False,
    allowed_worktree_roots: tuple[Path, ...] = (),
) -> RequirementSupervisor:
    store = control_store(workspace, requirement_id)
    protected = (workspace.root, workspace.project_root)
    # 用已校验的持久 Store 创建控制目录；不在 Task 可写目录安装控制面。
    worker_root = store.root.parent / "workers"
    preparation = OrchestrationStore(worker_root / "ledger")
    lease = preparation.acquire(f"prepare-{owner}")
    preparation.release(lease)
    tools: list[Path] = []
    try:
        tools.append(Path(codex_command()[0]).resolve().parent)
    except RuntimeError:
        pass
    for name in ("agent", "cursor-agent", "claude"):
        executable = shutil.which(name)
        if executable:
            tools.append(Path(executable).resolve().parent)
    launcher = WindowsAppContainerIsolation(controller_roots=(Path(__file__).resolve().parent,))
    workers = RuntimeWorkerPort(
        worker_root, requirement_id=requirement_id, runtime_factory=create_runtime,
        launcher=launcher, protected_roots=protected, readonly_tools=tuple(dict.fromkeys(tools)),
        allow_network=allow_network,
    )
    return RequirementSupervisor(
        store, owner=owner, workers=workers, runtimes=runtime_descriptors,
        max_workers=max_workers, protected_roots=protected,
        allowed_worktree_roots=allowed_worktree_roots,
    )


def run_supervisor(
    supervisor: RequirementSupervisor, *, request: PlanningRequest | None = None,
    timeout_seconds: float = 300, interval_seconds: float = 0.25,
    projection: TaskProjectionPump | None = None,
) -> dict[str, Any]:
    """有限前台服务；持续续租、收敛至候选/阻塞，不把退出码当作 Requirement 完成。"""
    if not 0 < timeout_seconds <= 86400 or not 0 < interval_seconds <= 5:
        raise WorkspaceError("编排服务时限必须在 0～86400 秒，轮询间隔在 0～5 秒")
    deadline = time.monotonic() + timeout_seconds
    try:
        supervisor.acquire()
        if request is not None:
            supervisor.initialize(request)
        while True:
            supervisor.renew()
            state = supervisor.tick()
            if projection is not None:
                projection.submit(state)
            nodes = state["data"]["nodes"].values()
            live = any(node["active_attempt_id"] is not None for node in nodes)
            if not live:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(interval_seconds)
    finally:
        # 正常返回也不遗留隐藏 Worker；close 在未知取消时仍保留占槽状态。
        try:
            supervisor.close(cancel_running=True)
        finally:
            if projection is not None:
                projection.close(timeout=1)
    return supervisor.status()


def configured_projection(
    workspace: WorkspaceStore, requirement_id: str,
) -> TaskProjectionPump | None:
    """映射必须是已持久初始请求中操作员明确提供的字段，不猜测外部 Task。"""
    store = control_store(workspace, requirement_id)
    source = store.snapshot()["data"]
    bindings = source.get("initial_request", {}).get("task_provider_bindings")
    if not bindings:
        return None
    provider = configured_task_provider(workspace.load(requirement_id)["meta"], workspace.project_root)
    if provider is None:
        return None
    return TaskProjectionPump(TaskProjection(
        OrchestrationStore(store.root.parent / "task-projection"), provider, bindings,
    ))
