from __future__ import annotations

import json
import subprocess
import sys
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


def _gates(tmp_path: Path, suite: dict[str, object]) -> GateStore:
    workspace = WorkspaceStore(tmp_path)
    requirement_id = workspace.create("Verification", task_provider=None)
    definition_path = GateStore.definition_path(requirement_id, 0)
    definition = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "phase": 0,
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


def test_gate_issue_live_revalidation_rejects_forged_pass_receipt(tmp_path: Path) -> None:
    gates = _gates(
        tmp_path,
        {
            "id": "local",
            "kind": "command",
            "commands": [[sys.executable, "-c", "raise SystemExit(7)"]],
        },
    )
    suite = gates.verification_suite("REQ-001", 0, "local", revision=SHA)
    timestamp = now_iso()
    forged = VerificationReceipt(
        receipt_id="local-forged",
        requirement_id="REQ-001",
        commit_sha=SHA,
        suite_id=suite.suite_id,
        suite_fingerprint=suite.fingerprint,
        issuer=suite.expected_issuer,
        run_id="forged-run",
        session_id="implementer",
        command=suite.command_summary,
        environment=PhaseVerificationRunner._local_environment(),
        started_at=timestamp,
        completed_at=timestamp,
        exit_code=0,
        status="PASS",
        summary="self-reported pass",
    )
    gates._write_verification_receipt(forged)
    gates.record_review_from_payload(
        "REQ-001",
        0,
        {
            "verification_receipt_refs": [forged.receipt_id],
            "implementation_session_ids": [forged.session_id],
            "implementation_run_ids": [forged.run_id],
            "verdict": "PASS",
            "resolved_findings": ["none claimed"],
        },
        reviewer_session_id="independent-reviewer",
    )

    with pytest.raises(PhaseGateError, match="Verification Suite local 失败"):
        gates.issue_from_payload(
            "REQ-001",
            0,
            {
                "acceptance_results": [
                    AcceptanceResult(
                        "AC-1", "PASS", "claimed", (forged.receipt_id,)
                    ).to_dict()
                ],
                "regression_summary": "claimed pass",
            },
            issued_by="codex:issuer",
        )

    assert not gates.path_for("REQ-001", 0).exists()


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

    def read(url: str) -> dict[str, object]:
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
    runner.revalidate("REQ-001", phase=0, receipt=receipt)

    assert receipt.issuer == "github-actions-api"
    assert receipt.run_id == "github-actions-42-attempt-1"
    assert receipt.source_url == "https://github.com/owner/repo/actions/runs/42"


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
