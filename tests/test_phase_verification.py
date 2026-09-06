from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from workspace_orchestrator.phase_gate import (
    AcceptanceResult,
    GateStore,
    PhaseGateError,
    VerificationReceipt,
    source_fingerprint,
)
from workspace_orchestrator.phase_verification import PhaseVerificationRunner
from workspace_orchestrator.workspace import WorkspaceStore, now_iso

SHA = "a" * 40
PLAN = "# Controlled verification\n"


@dataclass
class FakeGit:
    files: dict[tuple[str, str], str]
    head: str = SHA
    clean: bool = True

    def head_sha(self) -> str:
        return self.head

    def is_clean(self) -> bool:
        return self.clean

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        return ancestor_sha == descendant_sha

    def read_file_at(self, revision: str, relative_path: str) -> str:
        selected = self.head if revision == "HEAD" else revision
        return self.files[(selected, relative_path)]

    def list_files_at(self, revision: str, prefix: str) -> tuple[str, ...]:
        selected = self.head if revision == "HEAD" else revision
        return tuple(
            path
            for (sha, path) in self.files
            if sha == selected and path.startswith(prefix)
        )


def _gates(tmp_path: Path, suite: dict[str, object], *, phase: int = 0) -> GateStore:
    workspace = WorkspaceStore(tmp_path)
    requirement_id = workspace.create("Verification", task_provider=None)
    definition_path = GateStore.definition_path(requirement_id, phase)
    definition = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "phase": phase,
        "task_id": "TASK-1",
        "next_task_id": None,
        "plan_source_path": "plan.md",
        "plan_source_fingerprint": source_fingerprint(PLAN),
        "acceptance": [{"id": "AC-1", "description": "verified"}],
        "verification_suites": [suite],
    }
    files = {
        (SHA, "plan.md"): PLAN,
        (SHA, definition_path): json.dumps(definition),
    }
    return GateStore(workspace, FakeGit(files))


def _job(name: str, job_id: int, *, conclusion: str = "success") -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": 42,
        "head_sha": SHA,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
    }


def test_command_suite_is_executed_and_receipt_is_persisted(tmp_path: Path) -> None:
    suite = {
        "id": "local",
        "kind": "command",
        "commands": [[sys.executable, "-c", "print('verified')"]],
    }
    gates = _gates(tmp_path, suite)

    receipt = PhaseVerificationRunner(gates).run(
        "REQ-001", phase=0, suite_id="local", session_id="implementer"
    )

    assert receipt.issuer == "workspace-command-runner"
    assert receipt.commit_sha == SHA
    assert "verified" in receipt.summary
    assert receipt.command == json.dumps(
        ((sys.executable, "-c", "print('verified')"),),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert gates.read_verification_receipt("REQ-001", receipt.receipt_id) == receipt


def test_failed_command_suite_never_writes_a_pass_receipt(tmp_path: Path) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "local",
            "kind": "command",
            "commands": [[sys.executable, "-c", "raise SystemExit(7)"]],
        },
    )

    with pytest.raises(PhaseGateError, match="失败"):
        PhaseVerificationRunner(gates).run(
            "REQ-001", phase=0, suite_id="local", session_id="implementer"
        )

    assert not (gates.workspace_store.path_for("REQ-001") / "verification-receipts").exists()


def test_command_timeout_never_writes_a_pass_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "local",
            "kind": "command",
            "commands": [[sys.executable, "-c", "print('never reached')"]],
        },
    )

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="python", timeout=900)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(PhaseGateError, match="超过 900s"):
        PhaseVerificationRunner(gates).run(
            "REQ-001", phase=0, suite_id="local", session_id="implementer"
        )

    assert not (gates.workspace_store.path_for("REQ-001") / "verification-receipts").exists()


def test_gate_issue_local_command_receipt_does_not_rerun_suite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "local",
            "kind": "command",
            "commands": [[sys.executable, "-c", "print('already verified')"]],
        },
    )
    suite = gates.verification_suite("REQ-001", 0, "local", revision=SHA)
    timestamp = now_iso()
    receipt = VerificationReceipt(
        receipt_id="local-pass",
        requirement_id="REQ-001",
        commit_sha=SHA,
        suite_id=suite.suite_id,
        suite_fingerprint=suite.fingerprint,
        issuer=suite.expected_issuer,
        run_id="local-run",
        session_id="implementer",
        command=suite.command_summary,
        environment=PhaseVerificationRunner._local_environment(),
        started_at=timestamp,
        completed_at=timestamp,
        exit_code=0,
        status="PASS",
        summary="already verified",
    )
    gates._write_verification_receipt(receipt)
    gates.record_review_from_payload(
        "REQ-001",
        0,
        {
            "verification_receipt_refs": [receipt.receipt_id],
            "implementation_session_ids": [receipt.session_id],
            "implementation_run_ids": [receipt.run_id],
            "verdict": "PASS",
            "resolved_findings": ["none claimed"],
        },
        reviewer_session_id="independent-reviewer",
    )

    monkeypatch.setattr(
        PhaseVerificationRunner,
        "_execute_commands",
        lambda *_args, **_kwargs: pytest.fail("issue 不得重跑 local command suite"),
    )
    record = gates.issue_from_payload(
        "REQ-001",
        0,
        {
            "acceptance_results": [
                AcceptanceResult("AC-1", "PASS", "verified", (receipt.receipt_id,)).to_dict()
            ],
            "regression_summary": "stored local receipt verified",
        },
        issued_by="codex:issuer",
    )

    assert record.verification_receipt_refs == (receipt.receipt_id,)


def test_phase4_structured_receipt_fails_closed_without_authority(tmp_path: Path) -> None:
    gates = _gates(
        tmp_path,
        {"id": "local", "kind": "command", "commands": [["python", "-V"]]},
    )
    payload = {
        "requirement_id": "REQ-001",
        "phase": 4,
        "candidate_sha": SHA,
        "run_id": "run-1",
        "attempt": 1,
        "result": "PASS",
    }
    receipt = VerificationReceipt(
        "receipt-1", "REQ-001", SHA, "local", "a" * 64,
        "workspace-command-runner", "run-1", "implementer", "command", "env",
        now_iso(), now_iso(), 0, "PASS", "ok",
        structured_receipt=payload,
        signed_envelope={"payload": payload, "signature": "signed"},
        verification_plan={"requirement_id": "REQ-001", "phase": 4, "candidate_sha": SHA},
    )

    with pytest.raises(PhaseGateError, match="缺少受保护"):
        PhaseVerificationRunner(gates)._require_structured_authority(
            "REQ-001", phase=4, commit_sha=SHA, receipt=receipt,
            suite=gates.verification_suite("REQ-001", 0, "local", revision=SHA),
        )


def test_phase4_structured_receipt_uses_injected_authority(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []

    def verify(
        envelope: Mapping[str, object],
        plan: Mapping[str, object],
        run_id: str,
        attempt: int,
    ) -> dict[str, object]:
        calls.append((run_id, attempt))
        assert envelope["signature"] == "signed"
        assert plan["candidate_sha"] == SHA
        payload = envelope["payload"]
        assert isinstance(payload, Mapping)
        return dict(payload)

    gates = _gates(
        tmp_path,
        {"id": "local", "kind": "command", "commands": [["python", "-V"]]},
    )
    gates.structured_receipt_verifier = verify
    suite = gates.verification_suite("REQ-001", 0, "local", revision=SHA)
    receipt_started = now_iso()
    receipt_completed = now_iso()
    payload = {
        "requirement_id": "REQ-001", "phase": 4, "candidate_sha": SHA,
        "run_id": "run-1", "attempt": 2, "result": "PASS",
        "receipt_id": "receipt-1", "provider_id": suite.expected_issuer,
        "started_at": receipt_started,
        "completed_at": receipt_completed,
        "results": [{"suite_id": "local", "status": "PASS"}],
    }
    receipt = VerificationReceipt(
        "receipt-1", "REQ-001", SHA, "local", suite.fingerprint,
        suite.expected_issuer, "run-1", "implementer", suite.command_summary, "env",
        receipt_started, receipt_completed, 0, "PASS", "ok",
        structured_receipt=payload,
        signed_envelope={"payload": payload, "signature": "signed"},
        verification_plan={
            "requirement_id": "REQ-001", "phase": 4, "candidate_sha": SHA,
            "suites": [{"suite_id": "local", "argv": ["python", "-V"]}],
        },
        attempt=2,
    )

    PhaseVerificationRunner(gates)._require_structured_authority(
        "REQ-001", phase=4, commit_sha=SHA, receipt=receipt, suite=suite
    )
    assert calls == [("run-1", 2)]

    copied = VerificationReceipt.from_dict({**receipt.to_dict(), "receipt_id": "copied-receipt"})
    with pytest.raises(PhaseGateError, match="未精确绑定外层"):
        PhaseVerificationRunner(gates)._require_structured_authority(
            "REQ-001", phase=4, commit_sha=SHA, receipt=copied, suite=suite
        )


def test_phase4_attestation_revalidate_verifies_signature_and_live_ci_without_redispatch(
    tmp_path: Path,
) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "ci",
            "kind": "github-attestation",
            "attested_kind": "github-actions",
            "repository": "owner/repo",
            "workflow": "ci.yml",
            "required_event": "pull_request",
            "required_jobs": ["linux", "windows"],
        },
        phase=4,
    )
    suite = gates.verification_suite("REQ-001", 4, "ci", revision=SHA)
    # Signed attestors may preserve GitHub's RFC 3339 ``Z`` spelling while the
    # live reader canonicalizes the same instant to ``+00:00``.
    started = "2026-09-05T01:00:00Z"
    completed = "2026-09-05T01:05:00Z"
    payload = {
        "requirement_id": "REQ-001",
        "phase": 4,
        "candidate_sha": SHA,
        "run_id": "github-actions-42-attempt-1",
        "attempt": 1,
        "result": "PASS",
        "receipt_id": "ci-receipt",
        "provider_id": "github-actions-api",
        "started_at": started,
        "completed_at": completed,
        "results": [{"suite_id": "ci", "status": "PASS"}],
    }
    plan = {
        "requirement_id": "REQ-001",
        "phase": 4,
        "candidate_sha": SHA,
        "suites": [{
            "suite_id": "ci",
            "argv": ["github-actions", "owner/repo", "ci.yml", "pull_request", "linux", "windows"],
        }],
    }
    receipt = VerificationReceipt(
        "ci-receipt", "REQ-001", SHA, "ci", suite.fingerprint,
        suite.expected_issuer, "github-actions-42-attempt-1", "implementer",
        suite.command_summary, "GitHub Actions OIDC attestor", started, completed,
        0, "PASS", "attested",
        source_url="https://github.com/owner/repo/actions/runs/321",
        structured_receipt=payload,
        signed_envelope={"payload": payload, "signature": "signed"},
        verification_plan=plan,
    )
    verifier_calls: list[tuple[str, int]] = []

    def verify(
        _envelope: Mapping[str, object], _plan: Mapping[str, object],
        run_id: str, attempt: int,
    ) -> Mapping[str, object]:
        verifier_calls.append((run_id, attempt))
        return payload

    gates.structured_receipt_verifier = verify
    run = {
        "id": 42, "run_attempt": 1, "head_sha": SHA, "status": "completed",
        "event": "pull_request", "conclusion": "success",
        "path": ".github/workflows/ci.yml", "repository": {"full_name": "owner/repo"},
        "jobs_url": "https://api.github.com/repos/owner/repo/actions/runs/42/jobs",
        "html_url": "https://github.com/owner/repo/actions/runs/42",
        "run_started_at": "2026-09-05T01:00:00Z", "updated_at": "2026-09-05T01:05:00Z",
    }
    requests: list[str] = []

    def read(url: str) -> Mapping[str, object]:
        requests.append(url)
        if url.endswith("/actions/runs/42"):
            return run
        return {"total_count": 2, "jobs": [_job("linux", 101), _job("windows", 102)]}

    runner = PhaseVerificationRunner(
        gates,
        json_reader=read,
        structured_runner=lambda *_args: pytest.fail("revalidate 不得重新 dispatch workflow"),
    )
    runner.revalidate("REQ-001", phase=4, receipt=receipt)

    assert verifier_calls == [("github-actions-42-attempt-1", 1)]
    assert requests == [
        "https://api.github.com/repos/owner/repo/actions/runs/42",
        "https://api.github.com/repos/owner/repo/actions/runs/42/attempts/1/jobs?per_page=100&page=1",
    ]


def test_github_suite_imports_only_exact_sha_successful_required_jobs(
    tmp_path: Path,
) -> None:
    suite = {
        "id": "ci",
        "kind": "github-actions",
        "repository": "owner/repo",
        "workflow": "ci.yml",
        "required_event": "pull_request",
        "required_jobs": ["linux", "windows"],
    }
    gates = _gates(tmp_path, suite)
    run: dict[str, object] = {
        "id": 42,
        "run_number": 8,
        "run_attempt": 1,
        "head_sha": SHA,
        "status": "completed",
        "event": "pull_request",
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "owner/repo"},
        "jobs_url": "https://api.github.com/repos/owner/repo/actions/runs/42/jobs",
        "html_url": "https://github.com/owner/repo/actions/runs/42",
        "run_started_at": "2026-09-05T01:00:00Z",
        "updated_at": "2026-09-05T01:05:00Z",
    }

    requests: list[str] = []

    def read(url: str) -> dict[str, object]:
        requests.append(url)
        if "/runs?" in url:
            return {"workflow_runs": [run]}
        if url.endswith("/actions/runs/42"):
            return run
        return {
            "total_count": 2,
            "jobs": [_job("linux", 101), _job("windows", 102)],
        }

    runner = PhaseVerificationRunner(gates, json_reader=read)
    receipt = runner.run(
        "REQ-001", phase=0, suite_id="ci", session_id="implementer"
    )
    requests.clear()
    runner.revalidate("REQ-001", phase=0, receipt=receipt)

    assert receipt.issuer == "github-actions-api"
    assert receipt.run_id == "github-actions-42-attempt-1"
    assert receipt.source_url == "https://github.com/owner/repo/actions/runs/42"
    assert requests == [
        "https://api.github.com/repos/owner/repo/actions/runs/42",
        "https://api.github.com/repos/owner/repo/actions/runs/42/attempts/1/jobs?per_page=100&page=1",
    ]


def test_github_suite_rejects_missing_required_job(tmp_path: Path) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "ci",
            "kind": "github-actions",
            "repository": "owner/repo",
            "workflow": "ci.yml",
            "required_event": "pull_request",
            "required_jobs": ["linux", "windows"],
        },
    )

    def read(url: str) -> dict[str, object]:
        if "/runs?" in url:
            return {
                "workflow_runs": [
                    {
                        "id": 42,
                        "run_number": 8,
                        "run_attempt": 1,
                        "head_sha": SHA,
                        "status": "completed",
                        "event": "pull_request",
                        "conclusion": "success",
                        "path": ".github/workflows/ci.yml",
                        "repository": {"full_name": "owner/repo"},
                        "jobs_url": "https://api.github.com/repos/owner/repo/actions/runs/42/jobs",
                        "html_url": "https://github.com/owner/repo/actions/runs/42",
                        "run_started_at": "2026-09-05T01:00:00Z",
                        "updated_at": "2026-09-05T01:05:00Z",
                    }
                ]
            }
        return {"total_count": 1, "jobs": [_job("linux", 101)]}

    with pytest.raises(PhaseGateError, match="windows"):
        PhaseVerificationRunner(gates, json_reader=read).run(
            "REQ-001", phase=0, suite_id="ci", session_id="implementer"
        )


@pytest.mark.parametrize(
    "jobs",
    [
        [_job("linux", 101, conclusion="failure"), _job("linux", 102)],
        [_job("linux", 101), _job("linux", 102, conclusion="failure")],
    ],
)
def test_github_suite_rejects_duplicate_required_job_names(
    tmp_path: Path, jobs: list[dict[str, object]]
) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "ci",
            "kind": "github-actions",
            "repository": "owner/repo",
            "workflow": "ci.yml",
            "required_event": "pull_request",
            "required_jobs": ["linux"],
        },
    )
    run = {
        "id": 42,
        "run_number": 8,
        "run_attempt": 1,
        "head_sha": SHA,
        "status": "completed",
        "event": "pull_request",
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "owner/repo"},
        "jobs_url": "https://api.github.com/repos/owner/repo/actions/runs/42/jobs",
        "html_url": "https://github.com/owner/repo/actions/runs/42",
        "run_started_at": "2026-09-05T01:00:00Z",
        "updated_at": "2026-09-05T01:05:00Z",
    }

    def read(url: str) -> dict[str, object]:
        if "/runs?" in url:
            return {"workflow_runs": [run]}
        return {"total_count": len(jobs), "jobs": jobs}

    with pytest.raises(PhaseGateError, match="2 matches"):
        PhaseVerificationRunner(gates, json_reader=read).run(
            "REQ-001", phase=0, suite_id="ci", session_id="implementer"
        )


def test_github_suite_rejects_incomplete_job_pagination(tmp_path: Path) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "ci",
            "kind": "github-actions",
            "repository": "owner/repo",
            "workflow": "ci.yml",
            "required_event": "pull_request",
            "required_jobs": ["linux"],
        },
    )
    run = {
        "id": 42,
        "run_number": 8,
        "run_attempt": 1,
        "head_sha": SHA,
        "status": "completed",
        "event": "pull_request",
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "owner/repo"},
        "jobs_url": "https://api.github.com/repos/owner/repo/actions/runs/42/jobs",
        "html_url": "https://github.com/owner/repo/actions/runs/42",
        "run_started_at": "2026-09-05T01:00:00Z",
        "updated_at": "2026-09-05T01:05:00Z",
    }

    def read(url: str) -> dict[str, object]:
        if "/runs?" in url:
            return {"workflow_runs": [run]}
        if "&page=1" in url:
            return {"total_count": 2, "jobs": [_job("linux", 101)]}
        return {"total_count": 2, "jobs": []}

    with pytest.raises(PhaseGateError, match="分页不完整"):
        PhaseVerificationRunner(gates, json_reader=read).run(
            "REQ-001", phase=0, suite_id="ci", session_id="implementer"
        )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("event", "workflow_dispatch", "required_event"),
        (
            "jobs_url",
            "https://api.github.com/repos/other/repo/actions/runs/42/jobs",
            "jobs_url",
        ),
        ("html_url", "https://github.com/other/repo/actions/runs/42", "run URL"),
    ],
)
def test_github_suite_rejects_run_outside_committed_identity(
    tmp_path: Path, field: str, replacement: str, message: str
) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "ci",
            "kind": "github-actions",
            "repository": "owner/repo",
            "workflow": "ci.yml",
            "required_event": "pull_request",
            "required_jobs": ["linux"],
        },
    )
    run: dict[str, object] = {
        "id": 42,
        "run_number": 8,
        "run_attempt": 1,
        "head_sha": SHA,
        "status": "completed",
        "event": "pull_request",
        "conclusion": "success",
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "owner/repo"},
        "jobs_url": "https://api.github.com/repos/owner/repo/actions/runs/42/jobs",
        "html_url": "https://github.com/owner/repo/actions/runs/42",
        "run_started_at": "2026-09-05T01:00:00Z",
        "updated_at": "2026-09-05T01:05:00Z",
    }
    run[field] = replacement

    def read(url: str) -> dict[str, object]:
        if "/runs?" in url:
            return {"workflow_runs": [run]}
        return {"total_count": 1, "jobs": [_job("linux", 101)]}

    with pytest.raises(PhaseGateError, match=message):
        PhaseVerificationRunner(gates, json_reader=read).run(
            "REQ-001", phase=0, suite_id="ci", session_id="implementer"
        )
