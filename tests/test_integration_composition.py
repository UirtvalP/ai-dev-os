"""Phase 3 装配和 CLI 回归；复用真实临时 Git，绝不触及项目 main 或在线 Agent。"""

from __future__ import annotations

import copy
import itertools
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_git_workspaces import native

from workspace_orchestrator import integration_composition as composition
from workspace_orchestrator import orchestration_composition, product_cli
from workspace_orchestrator.delivery_guard import is_v2_delivery
from workspace_orchestrator.orchestration.contracts import (
    PlanningRequest,
    TaskSpec,
    VerificationCommand,
)
from workspace_orchestrator.orchestration.store import OrchestrationStore
from workspace_orchestrator.phase_gate import VerificationSuiteDefinition
from workspace_orchestrator.verification_provider import VerificationProviderError
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


@pytest.fixture
def project(tmp_path: Path) -> tuple[WorkspaceStore, PlanningRequest, str]:
    root = tmp_path / "repo"
    root.mkdir()
    native(root, "init", "-b", "main")
    native(root, "config", "user.name", "Fixture")
    native(root, "config", "user.email", "fixture@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    native(root, "add", "base.txt")
    native(root, "commit", "-m", "base")
    workspace = WorkspaceStore(root)
    requirement_id = workspace.create("复用组件交付", task_provider=None)
    request = PlanningRequest(requirement_id, "完成本批任务", (
        TaskSpec("A", "A", "实现 A", write_required=True),
        TaskSpec("B", "B", "实现 B", write_required=True),
    ), extra={"future-field": {"keep": True}})
    return workspace, request, native(root, "rev-parse", "HEAD")


def test_prepare_reuses_native_worktree_leases_and_preserves_unknown_fields(project: Any) -> None:
    workspace, request, base = project
    prepared = composition.prepare_git_request(workspace, request, expected_main_sha=base)
    again = composition.prepare_git_request(workspace, prepared, expected_main_sha=base)
    assert prepared == again and prepared.extra == request.extra
    assert prepared.tasks[0].worktree != prepared.tasks[1].worktree
    assert all(task.branch != "main" and Path(task.worktree).is_dir() for task in prepared.tasks)
    assert is_v2_delivery(workspace, request.requirement_id)
    assert native(workspace.project_root, "rev-parse", "main") == base
    assert workspace.load(request.requirement_id)["meta"]["status"] == "draft"


def test_prepare_rejects_unknown_paths_or_drift_before_worktree_allocation(project: Any) -> None:
    workspace, request, base = project
    unknown = replace(request, tasks=(replace(request.tasks[0], worktree=str(workspace.project_root)),))
    with pytest.raises(WorkspaceError, match="不接管"):
        composition.prepare_git_request(workspace, unknown, expected_main_sha=base)
    with pytest.raises(WorkspaceError, match="main 已漂移"):
        composition.prepare_git_request(workspace, request, expected_main_sha="a" * 40)
    assert composition.configured_git_workspaces(workspace).get(request.requirement_id, "A") is None
    assert not is_v2_delivery(workspace, request.requirement_id)


def test_done_requirement_cannot_allocate_new_v2_worktree(project: Any) -> None:
    workspace, request, base = project
    workspace.touch_meta(request.requirement_id, status="done")
    with pytest.raises(WorkspaceError, match="已完成"):
        composition.prepare_git_request(workspace, request, expected_main_sha=base)
    assert composition.configured_git_workspaces(workspace).get(request.requirement_id, "A") is None
    assert not is_v2_delivery(workspace, request.requirement_id)


def test_existing_phase2_plan_cannot_fall_back_to_legacy_completion(project: Any) -> None:
    workspace, request, _ = project
    ledger = orchestration_composition.control_store(workspace, request.requirement_id)
    lease = ledger.acquire("fixture")
    ledger.mutate(lease, lambda data: data.update(plan={"plan_id": "already-frozen"}))
    ledger.release(lease)
    assert is_v2_delivery(workspace, request.requirement_id)
    assert "delivery_profile" not in workspace.load(request.requirement_id)["meta"]


def test_existing_verification_configuration_is_used_without_a_second_registry(project: Any) -> None:
    workspace, _, _ = project
    (workspace.project_root / "pyproject.toml").write_text(
        '[tool.workspace-orchestrator.automation]\n'
        'verification-commands = [["{python}", "-m", "pytest"]]\n'
        'verification-timeout-seconds = 12\n', encoding="utf-8",
    )
    commands = composition.load_verification_commands(workspace)
    assert commands[0].command_id == "legacy-1" and commands[0].timeout_seconds == 12
    assert commands[0].argv[1:] == ("-m", "pytest")


def test_phase4_authority_uses_fixed_os_location_and_fails_closed_when_missing(
    project: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    workspace, _, _ = project
    fixed = tmp_path / "protected" / "verification-authority"
    monkeypatch.setattr(composition, "_authority_root", lambda: fixed)

    with pytest.raises(WorkspaceError, match="policy|authority|不可用"):
        composition.configured_phase_verification(workspace, phase=4)

    legacy = composition.configured_phase_verification(workspace, phase=3)
    assert legacy.gates.structured_receipt_verifier is None


def test_phase4_prefers_github_oidc_policy_without_local_authority_fallback(
    project: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    workspace, _, _ = project
    authority = tmp_path / "protected"
    authority.mkdir()
    (authority / "github-oidc-policy.json").write_text("{}", encoding="utf-8")
    (authority / "policy.json").write_text("{}", encoding="utf-8")
    (authority / "trust-store.json").write_text("{}", encoding="utf-8")
    calls: list[tuple[str, int]] = []

    class FakeVerifier:
        def __init__(self, policy: object) -> None:
            assert policy == "github-policy"

        def verify(
            self, envelope: object, plan: object, run_id: str, attempt: int,
        ) -> dict[str, object]:
            calls.append((run_id, attempt))
            return {"source": "github", "envelope": envelope, "plan": plan}

    class FakeClient:
        def __init__(self, policy: object) -> None:
            assert policy == "github-policy"

        def execute(self, **kwargs: object) -> object:
            assert kwargs == {
                "suite_id": kwargs["suite_id"],
                "candidate_sha": "1" * 40,
                "execution_kind": "command",
                "ci_workflow": None,
                "ci_event": None,
            }
            assert kwargs["suite_id"] in {"p4-suite", "p5-suite"}
            payload = {
                "receipt_id": "receipt-1",
                "run_id": "github-attestation-321-attempt-1",
                "attempt": 1,
                "started_at": "2026-09-06T00:00:00+00:00",
                "completed_at": "2026-09-06T00:01:00+00:00",
            }
            return SimpleNamespace(
                receipt=SimpleNamespace(**payload, to_dict=lambda: payload),
                plan=SimpleNamespace(to_dict=lambda: {"plan": True}),
                envelope=SimpleNamespace(to_dict=lambda: {"payload": payload}),
                attestor_run_id="321",
                attestor_run_attempt=1,
                source_url="https://github.com/owner/repo/actions/runs/321",
            )

    monkeypatch.setattr(composition, "_authority_root", lambda: authority)
    monkeypatch.setattr(composition, "_require_protected_authority", lambda *_args: None)
    monkeypatch.setattr(
        composition.GitHubAttestationTrustPolicy,
        "load",
        classmethod(lambda _cls, *_args, **_kwargs: "github-policy"),
    )
    monkeypatch.setattr(composition, "GitHubAttestationVerifier", FakeVerifier)
    monkeypatch.setattr(composition, "GitHubAttestorClient", FakeClient)
    monkeypatch.setattr(
        composition.AttestorPolicy,
        "load",
        classmethod(lambda *_args, **_kwargs: pytest.fail("不得降级到本地 policy")),
    )

    configured = composition.configured_phase_verification(workspace, phase=4)
    verifier = configured.gates.structured_receipt_verifier
    assert verifier is not None
    assert verifier({"signed": True}, {"candidate": True}, "123", 2)["source"] == "github"
    assert calls == [("123", 2)]
    structured_runner = configured.runner.structured_runner
    assert structured_runner is not None
    suite = VerificationSuiteDefinition(
        "p4-suite", "github-attestation", commands=(("python", "-V"),),
        attested_kind="command",
    )
    outer = structured_runner("REQ-020", 4, suite, "session-1", "1" * 40)
    assert outer.signed_envelope == {"payload": outer.structured_receipt}
    assert outer.source_url == "https://github.com/owner/repo/actions/runs/321"

    configured_phase5 = composition.configured_phase_verification(workspace, phase=5)
    phase5_runner = configured_phase5.runner.structured_runner
    assert phase5_runner is not None
    phase5_suite = VerificationSuiteDefinition(
        "p5-suite", "github-attestation", commands=(("python", "-V"),),
        attested_kind="command",
    )
    phase5 = phase5_runner("REQ-020", 5, phase5_suite, "session-1", "1" * 40)
    assert phase5.source_url == "https://github.com/owner/repo/actions/runs/321"


def test_phase4_missing_gh_is_unavailable_and_never_falls_back_to_local_keys(
    project: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    workspace, _, _ = project
    authority = tmp_path / "protected"
    authority.mkdir()
    (authority / "github-oidc-policy.json").write_text("{}", encoding="utf-8")
    (authority / "policy.json").write_text("{}", encoding="utf-8")
    (authority / "trust-store.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(composition, "_authority_root", lambda: authority)
    monkeypatch.setattr(composition, "_require_protected_authority", lambda *_args: None)
    monkeypatch.setattr(
        composition.GitHubAttestationTrustPolicy,
        "load",
        classmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(
            VerificationProviderError("attestor_unavailable", "GitHub CLI 不可用")
        )),
    )
    monkeypatch.setattr(
        composition.AttestorPolicy,
        "load",
        classmethod(lambda *_args, **_kwargs: pytest.fail("不得降级到本地 key")),
    )

    with pytest.raises(VerificationProviderError) as failure:
        composition.configured_phase_verification(workspace, phase=4)
    assert failure.value.code == "attestor_unavailable"


def test_operator_command_contract_preserves_extensions_and_rejects_empty(project: Any, tmp_path: Path) -> None:
    workspace, _, _ = project
    path = tmp_path / "commands.json"
    command = VerificationCommand("check", ("{python}", "-c", "print('ok')"), extra={"keep": "value"})
    path.write_text(json.dumps([command.to_dict()]), encoding="utf-8")
    assert composition.load_verification_commands(workspace, path) == (command,)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises((WorkspaceError, ValueError)):
        composition.load_verification_commands(workspace, path)


class BatchSupervisor:
    lease_ttl_seconds = 30

    def __init__(self) -> None:
        self.events: list[str] = []
        self.nodes = {task_id: {"status": "candidate_complete", "active_attempt_id": None}
                      for task_id in ("A", "B")}

    def acquire(self) -> None:
        self.events.append("acquire")

    def renew(self) -> None:
        self.events.append("renew")

    def status(self) -> dict[str, Any]:
        return {"data": {"nodes": copy.deepcopy(self.nodes)}}

    def verify_task(self, task_id: str, commands: Any, environment: Any) -> None:
        self.events.append(task_id)
        self.nodes[task_id]["status"] = "accepted" if task_id == "B" else "blocked"

    def close(self) -> None:
        self.events.append("close")


def test_batch_verification_collects_all_results_even_if_first_task_fails() -> None:
    supervisor = BatchSupervisor()
    result = composition.verify_candidates(supervisor, (VerificationCommand("check", ("fixture",)),),
                                           {"fixture": "only"})
    assert supervisor.events == ["acquire", "renew", "A", "renew", "B", "close"]
    assert result["data"]["nodes"]["A"]["status"] == "blocked"
    assert result["data"]["nodes"]["B"]["status"] == "accepted"


@pytest.mark.parametrize("kind", ["running", "duplicate", "unknown", "empty"])
def test_invalid_batch_is_rejected_before_any_verification(kind: str) -> None:
    supervisor = BatchSupervisor()
    tasks: tuple[str, ...] = ()
    if kind == "running":
        supervisor.nodes["B"]["active_attempt_id"] = "worker"
    elif kind == "duplicate":
        tasks = ("A", "A")
    elif kind == "unknown":
        tasks = ("missing",)
    else:
        supervisor.nodes.clear()
    with pytest.raises(WorkspaceError):
        composition.verify_candidates(supervisor, (), {"fixture": "only"}, task_ids=tasks)
    assert supervisor.events == ["acquire", "close"]


def test_product_prepare_outputs_request_and_never_starts_worker(
    project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, request, base = project
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request.to_dict()), encoding="utf-8")
    monkeypatch.setattr(product_cli, "discover_project_root", lambda root: root)
    assert product_cli.main(["orchestration", "prepare", request.requirement_id,
                             "--root", str(workspace.project_root), "--file", str(path),
                             "--expected-main", base]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tasks"][0]["branch"].startswith("ai-dev-os/task/")
    assert OrchestrationStore(workspace.path_for(request.requirement_id) / "orchestration" / "supervisor").snapshot()["data"] == {}


def test_product_merge_uses_trusted_environment_and_no_cli_pass_flag(
    project: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, request, base = project
    calls: list[Any] = []

    def integrate(*args: Any) -> Any:
        calls.append(args)
        return SimpleNamespace(to_dict=lambda: {"status": "merged", "completion_token": None})

    monkeypatch.setattr(product_cli, "discover_project_root", lambda root: root)
    monkeypatch.setattr(composition, "configured_integration", lambda *args: SimpleNamespace(integrate=integrate))
    monkeypatch.setattr(composition, "configured_verification", lambda *args: SimpleNamespace(environment={"actual": "fixture"}))
    commands = (VerificationCommand("check", ("fixture",)),)
    monkeypatch.setattr(composition, "load_verification_commands", lambda *args: commands)
    assert product_cli.main(["integration", "merge", request.requirement_id,
                             "--root", str(workspace.project_root), "--request-id", "retryable",
                             "--expected-main", base]) == 0
    assert calls == [(request.requirement_id, "retryable", base, commands, {"actual": "fixture"})]
    assert json.loads(capsys.readouterr().out)["completion_token"] is None


def test_product_post_merge_recovery_requires_explicit_stable_identity(
    project: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, request, _ = project
    calls: list[Any] = []

    def recover(*args: Any) -> Any:
        calls.append(args)
        return SimpleNamespace(to_dict=lambda: {"status": "merged", "receipt_id": "recovery-receipt"})

    monkeypatch.setattr(product_cli, "discover_project_root", lambda root: root)
    monkeypatch.setattr(composition, "configured_integration", lambda *args: SimpleNamespace(recover_post_merge=recover))
    assert product_cli.main(["integration", "recover-post-merge", request.requirement_id,
                             "--root", str(workspace.project_root), "--request-id", "original",
                             "--recovery-id", "operator-retry-1"]) == 0
    assert calls == [(request.requirement_id, "original", "operator-retry-1")]
    assert json.loads(capsys.readouterr().out)["receipt_id"] == "recovery-receipt"


def test_real_supervisor_git_verification_and_v1_review_compose_to_single_merge(
    project: Any, tmp_path: Path,
) -> None:
    """跨模块真实Git/Store/Review闭环；只有模型执行与命令进程为显式fixture。"""
    from test_legacy_verification import FixtureCommandPort
    from test_requirement_supervisor import FakeWorkers, runtime

    from workspace_orchestrator.integration.authority import WorkspaceReviewAuthority
    from workspace_orchestrator.integration.service import IntegrationService
    from workspace_orchestrator.integration.verification import LegacyVerificationAdapter
    from workspace_orchestrator.orchestration.supervisor import RequirementSupervisor
    from workspace_orchestrator.review import confirm_requirement_done

    workspace, request, base = project
    root = workspace.project_root
    remote = tmp_path / "remote.git"
    native(root, "init", "--bare", str(remote))
    native(root, "remote", "add", "origin", str(remote))
    native(root, "push", "origin", "main")
    prepared = composition.prepare_git_request(workspace, request, expected_main_sha=base)
    workspaces = composition.configured_git_workspaces(workspace)
    ticks = itertools.count()
    clock_base = time.time()

    def clock() -> float:
        return clock_base + next(ticks) * 0.001

    ledger = OrchestrationStore(
        workspace.path_for(request.requirement_id) / "orchestration" / "supervisor",
        clock=clock,
    )
    protected = (workspace.root, root)
    workers = FakeWorkers(ledger, protected)
    port = FixtureCommandPort()

    verifier = LegacyVerificationAdapter(
        protected_roots=protected, command_port=port, clock=clock,
    )
    controller = RequirementSupervisor(
        ledger, owner="integration-fixture", workers=workers, runtimes=lambda: (runtime(),),
        max_workers=2, protected_roots=protected, candidate_reader=workspaces.read_candidate,
        verification_executor=verifier, clock=clock,
    )
    controller.acquire()
    controller.initialize(prepared)
    controller.tick()
    for task in prepared.tasks:
        (Path(task.worktree) / (task.task_id + ".txt")).write_text(task.task_id + "\n", encoding="utf-8")
        sha, tree = workspaces.capture_candidate(task)
        workers.observe(task.task_id, "candidate_complete", candidate_sha=sha, candidate_tree=tree)
    controller.tick()
    commands = (VerificationCommand("check", ("fixture",)),)
    for task in prepared.tasks:
        controller.verify_task(task.task_id, commands, verifier.environment)
    supervisor = controller.status()["data"]
    assert supervisor["status"] == "ready_for_integration", supervisor
    controller.close()

    data = workspace.load(request.requirement_id)
    workspace.write_text(data["path"] / "requirement.md", data["requirement"].replace("- [ ]", "- [x]"))
    workspace.write_text(data["path"] / "intent.md", data["intent"].replace("：PARTIAL", "：PASS"))
    workspace.write_text(data["path"] / "state.md", "## Completed\n\n- 本批两个 Task 已验证\n")
    workspace.write_text(data["path"] / "verification.md", "## Verification\n\n- fixture\n\nStatus: PASS\n")
    service = IntegrationService(
        root, snapshot_reader=lambda req: ledger.snapshot(),
        review_authority=WorkspaceReviewAuthority(workspace, None, clock=clock), verifier=verifier,
        workspace_provider=workspaces, preserved_roots=(workspace.root,),
        clock=clock,
    )
    receipt = service.integrate(request.requirement_id, "one-merge", base, commands, verifier.environment)
    assert receipt.status == "merged"
    assert service.reconcile(request.requirement_id, "one-merge") == receipt
    assert native(root, "rev-parse", "main") == receipt.merged_sha
    assert (root / "A.txt").read_text(encoding="utf-8").strip() == "A"
    assert (root / "B.txt").read_text(encoding="utf-8").strip() == "B"
    assert len(port.calls) == 4  # 两个 Task + integration + post-merge。
    with pytest.raises(WorkspaceError, match="CompletionToken"):
        confirm_requirement_done(workspace, request.requirement_id, user_confirmed=True)
    assert workspace.load(request.requirement_id)["meta"]["status"] != "done"
