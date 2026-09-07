from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from workspace_orchestrator.github_attestation import (
    GitHubAttestationTrustPolicy,
    GitHubAttestorClient,
)
from workspace_orchestrator.orchestration.contracts import fingerprint
from workspace_orchestrator.verification_provider import (
    SuiteResult,
    VerificationPlan,
    VerificationProviderError,
    VerificationReceipt,
    VerificationSuite,
    _canonical,
)

SHA = "1" * 40
TREE = "2" * 40
POLICY_SHA = "3" * 64
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _policy() -> GitHubAttestationTrustPolicy:
    gh = Path("C:/trusted/gh.exe") if os.name == "nt" else Path("/trusted/gh")
    return GitHubAttestationTrustPolicy(
        repository="owner/repo",
        workflow_path=".github/workflows/phase-4-attestation.yml",
        workflow_ref="refs/heads/main",
        workflow_sha256="4" * 64,
        runner_path="scripts/github_attestation_runner.py",
        runner_sha256="5" * 64,
        attestor_policy_path=".github/phase-4-attestation-policy.json",
        attestor_policy_fingerprint=POLICY_SHA,
        action_sha="977bb373ede98d70efdf65b84cb5f73e068dcc2a",
        gh_path=gh,
        gh_sha256="6" * 64,
    )


def _artifact_payload() -> tuple[VerificationPlan, VerificationReceipt]:
    suite = VerificationSuite(
        "suite::1", "unit", ("python", "-V"), timeout_seconds=900,
    )
    plan = VerificationPlan(
        "ai-dev-os", "REQ-020", 4, SHA, TREE, POLICY_SHA, "7" * 64,
        (suite,), (suite.suite_id,),
    )
    result = SuiteResult(
        suite.suite_id, "unit", "PASS", 0, 1.0, EMPTY_SHA, EMPTY_SHA, "", "",
    )
    receipt = VerificationReceipt(
        1, "receipt-1", "ai-dev-os", "REQ-020", 4, SHA, TREE,
        plan.plan_fingerprint, POLICY_SHA, "github-attestation-321-attempt-2", 2,
        plan.environment_digest, "2026-09-06T00:00:00+00:00",
        "2026-09-06T00:01:00+00:00", (result,), "PASS", fingerprint([]),
        "workspace-command-runner", "github-oidc-v1",
    )
    return plan, receipt


class FakeGh:
    def __init__(self, plan: VerificationPlan, receipt: VerificationReceipt) -> None:
        self.plan = plan
        self.receipt = receipt
        self.calls: list[tuple[str, ...]] = []
        self.list_payload: object = [{
            "databaseId": 321,
            "attempt": 2,
            "displayTitle": "phase4-attestation-request-12345678",
            "event": "workflow_dispatch",
            "headBranch": "main",
            "status": "in_progress",
            "conclusion": None,
            "url": "https://github.com/owner/repo/actions/runs/321/attempts/2",
        }]

    def __call__(
        self, argv: Sequence[str], *, timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        call = tuple(argv)
        self.calls.append(call)
        if call[1:3] == ("workflow", "run") or call[1:3] == ("run", "watch"):
            return subprocess.CompletedProcess(call, 0, "", "")
        if call[1:3] == ("run", "list"):
            if call[call.index("--workflow") + 1] == "ci.yml":
                return subprocess.CompletedProcess(call, 0, json.dumps([{
                    "databaseId": 42, "attempt": 3, "event": "pull_request",
                    "headSha": SHA, "status": "completed", "conclusion": "success",
                }]), "")
            return subprocess.CompletedProcess(call, 0, json.dumps(self.list_payload), "")
        if call[1:3] == ("run", "view"):
            payload = dict(self.list_payload[0])  # type: ignore[index]
            payload.update(status="completed", conclusion="success")
            return subprocess.CompletedProcess(call, 0, json.dumps(payload), "")
        if call[1:3] == ("run", "download"):
            root = Path(call[call.index("--dir") + 1])
            (root / "phase4-plan.json").write_bytes(_canonical(self.plan.to_dict()))
            (root / "phase4-receipt.json").write_bytes(_canonical(self.receipt.to_dict()))
            return subprocess.CompletedProcess(call, 0, "", "")
        raise AssertionError(call)


def test_fake_gh_dispatch_locates_waits_and_downloads_exact_attestation() -> None:
    plan, receipt = _artifact_payload()
    gh = FakeGh(plan, receipt)
    client = GitHubAttestorClient(
        _policy(), command_runner=gh, request_id_factory=lambda: "request-12345678",
    )

    artifact = client.execute(
        suite_id="suite", candidate_sha=SHA, execution_kind="command",
    )

    dispatch = gh.calls[0]
    assert dispatch[:5] == (
        str(_policy().gh_path), "workflow", "run",
        ".github/workflows/phase-4-attestation.yml", "--repo",
    )
    assert "request_id=request-12345678" in dispatch
    assert dispatch[dispatch.index("--ref") + 1] == "main"
    download = next(call for call in gh.calls if call[1:3] == ("run", "download"))
    assert download[download.index("--name") + 1] == "phase4-attestation-321-2"
    assert artifact.plan == plan
    assert artifact.receipt == receipt
    assert artifact.envelope.attestor_run_id == "321"
    assert artifact.envelope.attestor_run_attempt == 2
    assert artifact.envelope.payload == receipt.to_dict()
    assert artifact.envelope.subject_sha256 == hashlib.sha256(
        _canonical(receipt.to_dict())
    ).hexdigest()


def test_fake_gh_duplicate_request_id_runs_fail_closed() -> None:
    plan, receipt = _artifact_payload()
    gh = FakeGh(plan, receipt)
    assert isinstance(gh.list_payload, list)
    gh.list_payload.append(dict(gh.list_payload[0], databaseId=322))
    client = GitHubAttestorClient(
        _policy(), command_runner=gh, request_id_factory=lambda: "request-12345678",
    )

    with pytest.raises(VerificationProviderError) as failure:
        client.execute(suite_id="suite", candidate_sha=SHA, execution_kind="command")
    assert failure.value.code == "ambiguous_run"
    assert not any(call[1:3] == ("run", "watch") for call in gh.calls)


def test_fake_gh_passes_exact_selected_ci_run_to_attestor() -> None:
    plan, receipt = _artifact_payload()
    gh = FakeGh(plan, receipt)
    client = GitHubAttestorClient(
        _policy(), command_runner=gh, request_id_factory=lambda: "request-12345678",
    )

    client.execute(
        suite_id="suite", candidate_sha=SHA, execution_kind="github-actions",
        ci_workflow="ci.yml", ci_event="pull_request",
    )

    dispatch = next(call for call in gh.calls if call[1:3] == ("workflow", "run"))
    assert "ci_run_id=42" in dispatch
    assert "ci_run_attempt=3" in dispatch


def test_fake_gh_missing_request_id_run_times_out_without_guessing() -> None:
    plan, receipt = _artifact_payload()
    gh = FakeGh(plan, receipt)
    gh.list_payload = []
    client = GitHubAttestorClient(
        _policy(), command_runner=gh, timeout_seconds=1, poll_interval_seconds=1,
        request_id_factory=lambda: "request-12345678", sleeper=lambda _seconds: None,
    )

    with pytest.raises(VerificationProviderError) as failure:
        client.execute(suite_id="suite", candidate_sha=SHA, execution_kind="command")
    assert failure.value.code == "attestor_timeout"


@pytest.mark.parametrize("mode, code", (("failure", "attestor_failed"), ("timeout", "attestor_timeout")))
def test_fake_gh_dispatch_failure_or_process_timeout_fails_closed(mode: str, code: str) -> None:
    def broken(
        argv: Sequence[str], *, timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 1, "", "dispatch denied")

    client = GitHubAttestorClient(
        _policy(), command_runner=broken,
        request_id_factory=lambda: "request-12345678",
    )
    with pytest.raises(VerificationProviderError) as failure:
        client.execute(suite_id="suite", candidate_sha=SHA, execution_kind="command")
    assert failure.value.code == code


@pytest.mark.parametrize("target", ("phase4-plan.json", "phase4-receipt.json"))
def test_fake_gh_rejects_unknown_plan_or_receipt_fields(target: str) -> None:
    plan, receipt = _artifact_payload()
    gh = FakeGh(plan, receipt)

    original = gh.__call__

    def malformed(
        argv: Sequence[str], *, timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        completed = original(argv, timeout=timeout)
        if tuple(argv)[1:3] == ("run", "download"):
            root = Path(argv[argv.index("--dir") + 1])
            raw = plan.to_dict() if target == "phase4-plan.json" else receipt.to_dict()
            raw["unexpected"] = True
            (root / target).write_text(json.dumps(raw), encoding="utf-8")
        return completed

    client = GitHubAttestorClient(
        _policy(), command_runner=malformed,
        request_id_factory=lambda: "request-12345678",
    )
    with pytest.raises(VerificationProviderError, match="字段"):
        client.execute(suite_id="suite", candidate_sha=SHA, execution_kind="command")
