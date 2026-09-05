"""把既有 Workspace、Supervisor、Review 与 Phase 3 Git 适配器接在一起。"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from .automation.state_sync import _verification_config
from .automation.task_attach import configured_task_provider
from .delivery_guard import mark_v2_delivery
from .integration.authority import WorkspaceReviewAuthority
from .integration.git_workspace import LocalGitWorkspaceProvider, TrustedGit
from .integration.service import IntegrationService
from .integration.verification import LegacyVerificationAdapter
from .orchestration.contracts import PlanningRequest, VerificationCommand, commands_fingerprint
from .orchestration.store import OrchestrationStore
from .orchestration.supervisor import RequirementSupervisor
from .workspace import WorkspaceError, WorkspaceStore


def configured_git_workspaces(workspace: WorkspaceStore) -> LocalGitWorkspaceProvider:
    git = TrustedGit(workspace.project_root)
    return LocalGitWorkspaceProvider(
        workspace.project_root, git.common_dir / "ai-dev-os-control" / "task-workspaces",
        workspace.project_root.parent / (workspace.project_root.name + ".tasks"),
    )


def configured_verification(workspace: WorkspaceStore) -> LegacyVerificationAdapter:
    return LegacyVerificationAdapter(protected_roots=(workspace.root, workspace.project_root))


def load_verification_commands(
    workspace: WorkspaceStore, path: Path | None = None,
) -> tuple[VerificationCommand, ...]:
    """复用 V1 项目配置；操作员也可明确给出已有 VerificationCommand JSON 契约。"""
    if path is not None:
        if path.stat().st_size > 1024 * 1024:
            raise WorkspaceError("验证命令 JSON 超出 1 MiB 限制")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise WorkspaceError("验证命令 JSON 必须是 VerificationCommand 对象数组")
        commands = tuple(VerificationCommand.from_dict(item) for item in raw)
    else:
        configured, timeout = _verification_config(workspace.project_root)
        commands = tuple(VerificationCommand(f"legacy-{index}", argv, math.ceil(timeout))
                         for index, argv in enumerate(configured, 1))
    commands_fingerprint(commands)
    if not commands:
        raise WorkspaceError("没有已配置验证命令，不能将空验证视为通过")
    return commands


def prepare_git_request(
    workspace: WorkspaceStore, request: PlanningRequest, *, expected_main_sha: str,
) -> PlanningRequest:
    """只分配原始授权 Task；重复命令恢复相同租约，不接受模型指定的新路径。"""
    workspace.load(request.requirement_id)
    request.validate()
    provider = configured_git_workspaces(workspace)
    with provider.git.writer():
        if provider.git.resolve("refs/heads/main") != expected_main_sha:
            raise WorkspaceError("main 已漂移，不能在旧基线上创建工作树")
        state = OrchestrationStore(
            workspace.path_for(request.requirement_id) / "orchestration" / "supervisor",
        ).snapshot()["data"]
        if state.get("plan"):
            raise WorkspaceError("已有冻结计划，请恢复执行；不能重新分配工作树改写授权")
        tasks = []
        for task in request.tasks:
            previous = provider.get(request.requirement_id, task.task_id)
            if (task.worktree is not None or task.branch is not None) and (
                previous is None or (task.worktree, task.branch) != (previous.worktree, previous.branch)
            ):
                raise WorkspaceError("prepare 不接管未知 Task 路径或分支；只恢复本系统持久租约")
        mark_v2_delivery(workspace, request.requirement_id)
        for task in request.tasks:
            lease = provider.ensure(request.requirement_id, task.task_id, base_sha=expected_main_sha)
            tasks.append(replace(task, worktree=lease.worktree, branch=lease.branch))
        return replace(request, tasks=tuple(tasks))


def configured_integration(workspace: WorkspaceStore, requirement_id: str) -> IntegrationService:
    workspace.load(requirement_id)
    provider = configured_task_provider(workspace.load(requirement_id)["meta"], workspace.project_root)
    preserved = [workspace.root]
    venv = workspace.project_root / ".venv"
    if venv.exists():
        preserved.append(venv)
    return IntegrationService(
        workspace.project_root,
        snapshot_reader=lambda req: OrchestrationStore(
            workspace.path_for(req) / "orchestration" / "supervisor",
        ).snapshot(),
        review_authority=WorkspaceReviewAuthority(workspace, provider),
        verifier=configured_verification(workspace),
        workspace_provider=configured_git_workspaces(workspace),
        preserved_roots=tuple(preserved),
    )


def verify_candidates(
    supervisor: RequirementSupervisor, commands: tuple[VerificationCommand, ...],
    environment: dict[str, str], *, task_ids: tuple[str, ...] = (),
    refresh: bool = False,
) -> dict[str, Any]:
    """一批验证沿用单写者租约；长命令期间续租，未通过不会自动合并或完成。"""
    stop = threading.Event()
    failures: list[Exception] = []

    def renew() -> None:
        while not stop.wait(min(5.0, supervisor.lease_ttl_seconds / 3)):
            try:
                supervisor.renew()
            except Exception as exc:  # noqa: BLE001 -- 丢失租约后不可再接纳本轮状态。
                failures.append(exc)
                return

    supervisor.acquire()
    heartbeat = threading.Thread(target=renew, name="verification-lease", daemon=True)
    try:
        data = supervisor.status()["data"]
        nodes = data.get("nodes", {})
        if any(node.get("active_attempt_id") is not None for node in nodes.values()):
            raise WorkspaceError("仍有活动或未知 Worker；整批实现结束后再进行统一验证")
        allowed = {"candidate_complete", "accepted"} if refresh else {"candidate_complete"}
        selected = task_ids or tuple(task_id for task_id, node in nodes.items()
                                     if node["status"] in allowed)
        if (not selected or len(set(selected)) != len(selected)
                or any(task_id not in nodes or nodes[task_id]["status"] not in allowed
                       for task_id in selected)):
            raise WorkspaceError("验证目标必须是已终止 Worker 的非空、唯一候选列表")
        heartbeat.start()
        for task_id in selected:
            if failures:
                raise WorkspaceError(f"验证续租失败：{failures[0]}")
            supervisor.renew()
            if refresh:
                supervisor.verify_task(task_id, commands, environment, refresh=True)
            else:
                supervisor.verify_task(task_id, commands, environment)
        if failures:
            raise WorkspaceError(f"验证续租失败：{failures[0]}")
        return supervisor.status()
    finally:
        stop.set()
        if heartbeat.ident is not None:
            heartbeat.join(timeout=60)
            if heartbeat.is_alive():
                raise WorkspaceError("验证续租线程未退出，保留执行状态等待恢复")
        supervisor.close()
