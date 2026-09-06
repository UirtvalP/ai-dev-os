from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from scripts import github_attestation_runner as runner

SHA = "1" * 40
TREE = "2" * 40


def _policy(suite: dict[str, object]) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "policy_id": "policy",
        "project_id": "ai-dev-os",
        "requirement_id": "REQ-020",
        "phase": 4,
        "repository": "UirtvalP/ai-dev-os",
        "workflow_path": ".github/workflows/phase-4-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "suites": {"suite": suite},
    }
    return {**unsigned, "policy_fingerprint": runner.fingerprint(unsigned)}


def test_command_receipt_uses_real_process_facts_and_never_infers_pass_from_job(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    output = tmp_path / "output"
    candidate.mkdir()
    output.mkdir()
    policy = _policy({
        "kind": "command",
        "provider_id": "workspace-command-runner",
        "commands": [{
            "suite_type": "unit",
            "argv": [sys.executable, "-c", "import sys; print('real'); sys.exit(7)"],
        }],
    })

    plan, receipt = runner.build_receipt(
        policy,
        suite_id="suite",
        candidate_sha=SHA,
        candidate_tree_sha=TREE,
        github_run_id="123",
        github_run_attempt=2,
        token="token",
        output=output,
        candidate_root=candidate,
    )

    assert plan["required_suite_ids"] == ["suite::1"]
    assert receipt["result"] == "FAIL"
    result = receipt["results"][0]
    assert result["status"] == "FAIL"
    assert result["returncode"] == 7
    stdout = f"real{os.linesep}".encode()
    assert result["stdout_preview"] == stdout.decode()
    assert result["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()
    assert isinstance(result["duration_seconds"], int)


def test_command_receipt_records_process_unavailable_as_error(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    policy = _policy({
        "kind": "command",
        "provider_id": "workspace-command-runner",
        "commands": [{"suite_type": "unit", "argv": ["definitely-missing-command"]}],
    })

    _, receipt = runner.build_receipt(
        policy,
        suite_id="suite",
        candidate_sha=SHA,
        candidate_tree_sha=TREE,
        github_run_id="123",
        github_run_attempt=1,
        token="token",
        output=tmp_path,
        candidate_root=candidate,
    )

    assert receipt["result"] == "FAIL"
    assert receipt["results"][0]["status"] == "ERROR"
    assert receipt["results"][0]["error_code"] == "process_unavailable"


def test_github_suite_requires_exact_completed_run_and_each_required_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy({
        "kind": "github-actions",
        "provider_id": "github-actions-api",
        "repository": "UirtvalP/ai-dev-os",
        "workflow": "ci.yml",
        "required_event": "pull_request",
        "required_jobs": ["ubuntu / Python 3.11"],
    })

    def github_json(url: str, _token: str) -> dict[str, object]:
        if "/jobs?" in url:
            return {
                "total_count": 1,
                "jobs": [{
                    "name": "ubuntu / Python 3.11",
                    "status": "completed",
                    "conclusion": "success",
                }],
            }
        return {
            "id": 456,
            "run_attempt": 3,
            "head_sha": SHA,
            "path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": "UirtvalP/ai-dev-os"},
            "html_url": "https://github.com/UirtvalP/ai-dev-os/actions/runs/456",
            "run_started_at": "2026-09-06T00:00:00Z",
            "updated_at": "2026-09-06T00:01:00Z",
        }

    monkeypatch.setattr(runner, "github_json", github_json)
    _, receipt = runner.build_receipt(
        policy,
        suite_id="suite",
        candidate_sha=SHA,
        candidate_tree_sha=TREE,
        github_run_id="123",
        github_run_attempt=1,
        token="token",
        output=tmp_path,
        ci_run_id="456",
        ci_run_attempt=3,
    )
    assert receipt["result"] == "PASS"
    assert receipt["results"][0]["status"] == "PASS"
    assert receipt["results"][0]["artifacts"][0]["path"] == "github-ci-evidence.json"


def test_workflow_and_repository_policy_pin_trusted_attestor_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/phase-4-attestation.yml").read_text(encoding="utf-8")
    policy = json.loads(
        (root / ".github/phase-4-attestation-policy.json").read_text(encoding="utf-8")
    )
    claimed = policy.pop("policy_fingerprint")

    assert "workflow_dispatch:" in workflow
    assert "run-name: phase4-attestation-${{ inputs.request_id }}" in workflow
    assert "request_id:" in workflow
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "python trusted/scripts/github_attestation_runner.py" in workflow
    assert "candidate/scripts/github_attestation_runner.py" not in workflow
    assert (
        "actions/attest-build-provenance@"
        "977bb373ede98d70efdf65b84cb5f73e068dcc2a"
    ) in workflow
    assert claimed == runner.fingerprint(policy)
    assert policy["external_authority"]["fallback"] == "forbidden"
    assert "gh_sha256" in policy["external_authority"]["required_pins"]
    pins = policy["external_authority"]["pinned_values"]
    assert pins["workflow_sha256"] == hashlib.sha256(workflow.encode()).hexdigest()
    runner_text = (root / "scripts/github_attestation_runner.py").read_text(encoding="utf-8")
    assert pins["runner_sha256"] == hashlib.sha256(runner_text.encode()).hexdigest()
    installer = (root / "scripts/install_github_attestation_trust.ps1").read_text(
        encoding="utf-8",
    )
    assert pins["workflow_sha256"] in installer
    assert pins["runner_sha256"] in installer
    assert claimed in installer


def test_main_returns_failure_after_writing_a_failed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    output = tmp_path / "output"
    candidate.mkdir()
    policy = _policy({
        "kind": "command",
        "provider_id": "workspace-command-runner",
        "commands": [{
            "suite_type": "unit",
            "argv": [sys.executable, "-c", "raise SystemExit(2)"],
        }],
    })
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(runner, "candidate_tree", lambda *_args: TREE)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_REPOSITORY", "UirtvalP/ai-dev-os")
    monkeypatch.setattr(sys, "argv", [
        "github_attestation_runner.py",
        "--policy", str(policy_path),
        "--suite-id", "suite",
        "--candidate-sha", SHA,
        "--candidate-root", str(candidate),
        "--output", str(output),
    ])

    assert runner.main() == 1
    assert json.loads((output / "phase4-receipt.json").read_text())["result"] == "FAIL"
    assert (output / "phase4-envelope.json").is_file()
