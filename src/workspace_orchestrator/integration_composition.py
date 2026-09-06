"""把既有 Workspace、Supervisor、Review 与 Phase 3 Git 适配器接在一起。"""

from __future__ import annotations

import json
import math
import os
import platform
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from .automation.state_sync import _verification_config
from .automation.task_attach import configured_task_provider
from .delivery_guard import mark_v2_delivery
from .github_attestation import (
    GitHubAttestationTrustPolicy,
    GitHubAttestationVerifier,
    GitHubAttestorClient,
)
from .integration.authority import WorkspaceReviewAuthority
from .integration.git_workspace import LocalGitWorkspaceProvider, TrustedGit
from .integration.service import IntegrationService
from .integration.verification import LegacyVerificationAdapter
from .orchestration.contracts import (
    PlanningRequest,
    VerificationCommand,
    commands_fingerprint,
)
from .orchestration.store import OrchestrationStore
from .orchestration.supervisor import RequirementSupervisor
from .phase_gate import GateStore, PhaseGateError, VerificationReceipt
from .phase_verification import PhaseVerificationRunner
from .verification_provider import (
    ArtifactConstraint,
    AttestorPolicy,
    SignedReceiptEnvelope,
    TrustStore,
    VerificationPlan,
    VerificationSuite,
)
from .workspace import WorkspaceError, WorkspaceStore


@dataclass(frozen=True, slots=True)
class ConfiguredPhaseVerification:
    gates: GateStore
    runner: PhaseVerificationRunner


def _authority_root() -> Path:
    """固定 OS 管理员级位置；repository、HOME 与环境变量均不能改写信任根。"""

    if platform.system() == "Windows":
        return Path("C:/ProgramData/ai-dev-os/verification-authority")
    return Path("/etc/ai-dev-os/verification-authority")


def _require_protected_authority(root: Path, *filenames: str) -> None:
    paths = (root, *(root / filename for filename in filenames))
    if any(not path.exists() for path in paths):
        raise PhaseGateError("Phase 4+ 受保护 authority 配置不可用")
    if any(_worker_can_write(path) for path in paths):
        raise PhaseGateError("Phase 4+ authority 可被当前 Worker 写入，拒绝信任")
    if os.name != "nt":
        for path in paths:
            stat = path.stat()
            if stat.st_uid != 0 or stat.st_mode & 0o022:
                raise PhaseGateError("Phase 4+ authority owner/mode 不受保护")


def _worker_can_write(path: Path) -> bool:
    """Probe effective permissions; ``os.access`` does not honor Windows ACLs reliably."""

    try:
        if path.is_dir():
            with tempfile.NamedTemporaryFile(dir=path, prefix=".authority-probe-"):
                pass
        else:
            with path.open("r+b"):
                pass
    except PermissionError:
        return False
    except OSError:
        # Unknown access failures are not proof of a protected authority.
        return True
    return True


def _plan_from_mapping(payload: Mapping[str, object]) -> VerificationPlan:
    try:
        raw_suites = cast(list[dict[str, Any]], payload["suites"])
        suites = tuple(
            VerificationSuite(
                suite_id=str(item["suite_id"]),
                suite_type=str(item["suite_type"]),  # type: ignore[arg-type]
                argv=tuple(str(value) for value in item["argv"]),
                timeout_seconds=int(item["timeout_seconds"]),
                cwd=str(item["cwd"]),
                environment_allowlist=tuple(str(value) for value in item["environment_allowlist"]),
                environment=dict(item["environment"]),
                artifacts=tuple(
                    ArtifactConstraint(
                        str(artifact["path"]), bool(artifact["required"]),
                        artifact.get("max_bytes"),
                    )
                    for artifact in item["artifacts"]
                ),
                requires_network=bool(item["requires_network"]),
                network_reader_id=item.get("network_reader_id"),
            )
            for item in raw_suites
        )
        return VerificationPlan(
            str(payload["project_id"]), str(payload["requirement_id"]),
            int(cast(int, payload["phase"])),
            str(payload["candidate_sha"]), str(payload["candidate_tree"]),
            str(payload["policy_fingerprint"]), str(payload["environment_digest"]), suites,
            tuple(str(value) for value in cast(list[object], payload["required_suite_ids"])),
            str(payload["mode"]),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PhaseGateError(f"结构化 Verification Plan 无效：{exc}") from exc


def configured_phase_verification(
    workspace: WorkspaceStore, *, phase: int,
) -> ConfiguredPhaseVerification:
    """装配 Phase CLI；Phase 4+ 的 trust/policy/key 只能来自固定 repository 外位置。"""

    if phase < 4:
        gates = GateStore(workspace)
        return ConfiguredPhaseVerification(gates, PhaseVerificationRunner(gates))
    authority = _authority_root()
    github_policy_path = authority / "github-oidc-policy.json"
    if github_policy_path.exists():
        _require_protected_authority(authority, "github-oidc-policy.json")
        github_policy = GitHubAttestationTrustPolicy.load(
            github_policy_path, repository=workspace.project_root,
        )
        github_verifier = GitHubAttestationVerifier(github_policy)
        github_client = GitHubAttestorClient(github_policy)

        def verify_github(
            raw_envelope: Mapping[str, object], raw_plan: Mapping[str, object],
            run_id: str, attempt: int,
        ) -> dict[str, object]:
            return dict(github_verifier.verify(raw_envelope, raw_plan, run_id, attempt))

        gates = GateStore(workspace, structured_receipt_verifier=verify_github)

        def execute_github(
            requirement_id: str,
            requested_phase: int,
            suite: object,
            session_id: str,
            commit_sha: str,
        ) -> VerificationReceipt:
            from .phase_gate import VerificationSuiteDefinition

            if (
                requested_phase != 4
                or not isinstance(suite, VerificationSuiteDefinition)
                or suite.kind != "github-attestation"
            ):
                raise PhaseGateError("GitHub OIDC attestor 仅执行 Phase 4 github-attestation Suite")
            artifact = github_client.execute(
                suite_id=suite.suite_id,
                candidate_sha=commit_sha,
                execution_kind=suite.execution_kind,
                ci_workflow=suite.workflow,
                ci_event=suite.required_event,
            )
            structured = artifact.receipt.to_dict()
            return VerificationReceipt(
                receipt_id=artifact.receipt.receipt_id,
                requirement_id=requirement_id,
                commit_sha=commit_sha,
                suite_id=suite.suite_id,
                suite_fingerprint=suite.fingerprint,
                issuer=suite.expected_issuer,
                run_id=artifact.receipt.run_id,
                session_id=session_id,
                command=suite.command_summary,
                environment="GitHub Actions OIDC attestor",
                started_at=artifact.receipt.started_at,
                completed_at=artifact.receipt.completed_at,
                exit_code=0,
                status="PASS",
                summary=(
                    f"GitHub OIDC attestation run {artifact.attestor_run_id} "
                    f"attempt {artifact.attestor_run_attempt} verified"
                ),
                source_url=artifact.source_url,
                structured_receipt=structured,
                signed_envelope=artifact.envelope.to_dict(),
                verification_plan=artifact.plan.to_dict(),
                attempt=artifact.receipt.attempt,
            )

        return ConfiguredPhaseVerification(
            gates, PhaseVerificationRunner(gates, structured_runner=execute_github),
        )

    _require_protected_authority(authority, "policy.json", "trust-store.json")
    policy = AttestorPolicy.load(authority / "policy.json", repository=workspace.project_root)
    trust = TrustStore.load(authority / "trust-store.json", repository=workspace.project_root)
    pairs = tuple(
        (attestor_id, key_id)
        for attestor_id, key_ids in sorted(trust.attestor_keys.items())
        for key_id in sorted(key_ids)
        if key_id in policy.allowed_key_ids
    )
    if len(pairs) != 1:
        raise PhaseGateError("Phase 4+ authority 必须唯一确定 attestor/key 配对")

    def verify(
        raw_envelope: Mapping[str, object], raw_plan: Mapping[str, object],
        run_id: str, attempt: int,
    ) -> dict[str, object]:
        plan = _plan_from_mapping(raw_plan)
        envelope = SignedReceiptEnvelope.from_dict(raw_envelope)
        return dict(trust.verify(
            envelope, plan=plan, policy=policy,
            expected_run_id=run_id, expected_attempt=attempt,
        ))

    gates = GateStore(workspace, structured_receipt_verifier=verify)

    def execute(*_args: object, **_kwargs: object) -> VerificationReceipt:
        raise PhaseGateError(
            "Phase 4+ 独立 attestor 服务未配置；Worker 不得加载私钥或自签 Receipt"
        )

    return ConfiguredPhaseVerification(
        gates, PhaseVerificationRunner(gates, structured_runner=execute),
    )


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
    plain_gates = GateStore(workspace)
    current_task = workspace.load(requirement_id)["meta"].get("requirement_task_id")
    current_definition = next(
        (item for item in plain_gates.definitions(requirement_id) if item.task_id == current_task),
        None,
    ) if plain_gates.is_required(requirement_id) else None
    phase_gates = configured_phase_verification(
        workspace, phase=current_definition.phase if current_definition is not None else 0,
    ).gates
    preserved = [workspace.root]
    venv = workspace.project_root / ".venv"
    if venv.exists():
        preserved.append(venv)
    return IntegrationService(
        workspace.project_root,
        snapshot_reader=lambda req: OrchestrationStore(
            workspace.path_for(req) / "orchestration" / "supervisor",
        ).snapshot(),
        review_authority=WorkspaceReviewAuthority(
            workspace, provider, phase_gates=phase_gates,
        ),
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
