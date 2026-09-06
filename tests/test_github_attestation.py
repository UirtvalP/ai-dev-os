from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from scripts import github_attestation_runner as runner
from workspace_orchestrator.github_attestation import (
    GitHubAttestationTrustPolicy,
    GitHubAttestationVerifier,
    GitHubOIDCAttestationEnvelope,
)
from workspace_orchestrator.verification_provider import VerificationProviderError

CANDIDATE_SHA = "1" * 40
CANDIDATE_TREE = "2" * 40
WORKFLOW_SHA = "3" * 40
ACTION_SHA = "977bb373ede98d70efdf65b84cb5f73e068dcc2a"


def _repository_policy() -> tuple[dict[str, object], bytes]:
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "policy_id": "policy",
        "project_id": "ai-dev-os",
        "requirement_id": "REQ-020",
        "phase": 4,
        "repository": "UirtvalP/ai-dev-os",
        "workflow_path": ".github/workflows/phase-4-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "suites": {
            "suite": {
                "kind": "command",
                "provider_id": "workspace-command-runner",
                "commands": [{
                    "suite_type": "unit",
                    "argv": [sys.executable, "-c", "print('verified')"],
                }],
            },
        },
    }
    payload = {**unsigned, "policy_fingerprint": runner.fingerprint(unsigned)}
    return payload, runner.canonical(payload)


def _policy(
    tmp_path: Path, workflow: bytes, script: bytes, policy_fingerprint: str,
) -> GitHubAttestationTrustPolicy:
    gh = tmp_path / "protected" / "gh"
    gh.parent.mkdir()
    gh.write_bytes(b"fixed-gh")
    return GitHubAttestationTrustPolicy(
        repository="UirtvalP/ai-dev-os",
        workflow_path=".github/workflows/phase-4-attestation.yml",
        workflow_ref="refs/heads/main",
        workflow_sha256=hashlib.sha256(workflow).hexdigest(),
        runner_path="scripts/github_attestation_runner.py",
        runner_sha256=hashlib.sha256(script).hexdigest(),
        attestor_policy_path=".github/phase-4-attestation-policy.json",
        attestor_policy_fingerprint=policy_fingerprint,
        action_sha=ACTION_SHA,
        gh_path=gh,
        gh_sha256=hashlib.sha256(gh.read_bytes()).hexdigest(),
    )


def _certificate(envelope: dict[str, object]) -> dict[str, object]:
    identity = (
        "https://github.com/UirtvalP/ai-dev-os/"
        ".github/workflows/phase-4-attestation.yml@refs/heads/main"
    )
    invocation = (
        "https://github.com/UirtvalP/ai-dev-os/actions/runs/"
        f"{envelope['attestor_run_id']}/attempts/{envelope['attestor_run_attempt']}"
    )
    return {
        "subjectAlternativeName": identity,
        "issuer": "https://token.actions.githubusercontent.com",
        "buildSignerURI": identity,
        "buildSignerDigest": WORKFLOW_SHA,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": "https://github.com/UirtvalP/ai-dev-os",
        "sourceRepositoryDigest": WORKFLOW_SHA,
        "sourceRepositoryRef": "refs/heads/main",
        "buildConfigURI": identity,
        "buildConfigDigest": WORKFLOW_SHA,
        "buildTrigger": "workflow_dispatch",
        "runInvocationURI": invocation,
    }


def test_policy_reports_missing_pinned_gh_as_unavailable_without_path_fallback(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    authority = tmp_path / "authority"
    repository.mkdir()
    authority.mkdir()
    payload = {
        "schema_version": 1,
        "repository": "UirtvalP/ai-dev-os",
        "workflow_path": ".github/workflows/phase-4-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "workflow_sha256": "a" * 64,
        "runner_path": "scripts/github_attestation_runner.py",
        "runner_sha256": "b" * 64,
        "attestor_policy_path": ".github/phase-4-attestation-policy.json",
        "attestor_policy_fingerprint": "c" * 64,
        "action_sha": ACTION_SHA,
        "gh_path": str((authority / "missing-gh").resolve()),
        "gh_sha256": "d" * 64,
    }
    path = authority / "github-oidc-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VerificationProviderError) as failure:
        GitHubAttestationTrustPolicy.load(path, repository=repository)
    assert failure.value.code == "attestor_unavailable"


def test_verified_attestation_binds_receipt_run_candidate_and_pinned_remote_files(
    tmp_path: Path,
) -> None:
    repository_policy, repository_policy_bytes = _repository_policy()
    policy_fingerprint = str(repository_policy["policy_fingerprint"])
    workflow = f"uses: actions/attest-build-provenance@{ACTION_SHA}\n".encode()
    script = b"trusted runner\n"
    policy = _policy(tmp_path, workflow, script, policy_fingerprint)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    plan, receipt = runner.build_receipt(
        repository_policy,
        suite_id="suite",
        candidate_sha=CANDIDATE_SHA,
        candidate_tree_sha=CANDIDATE_TREE,
        github_run_id="123",
        github_run_attempt=2,
        token="token",
        output=output,
        candidate_root=candidate,
    )
    envelope = runner.build_envelope(repository_policy, receipt, run_id="123", attempt=2)

    encoded = {
        policy.workflow_path: workflow,
        policy.runner_path: script,
        policy.attestor_policy_path: repository_policy_bytes,
    }

    def json_reader(url: str) -> dict[str, object]:
        if "/actions/runs/" in url:
            return {
                "id": 123,
                "run_attempt": 2,
                "head_sha": WORKFLOW_SHA,
                "head_branch": "main",
                "path": policy.workflow_path,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": policy.repository},
            }
        if f"/git/commits/{CANDIDATE_SHA}" in url:
            return {"sha": CANDIDATE_SHA, "tree": {"sha": CANDIDATE_TREE}}
        marker = "/contents/"
        if marker in url:
            path = unquote(url.split(marker, 1)[1].split("?", 1)[0])
            return {
                "encoding": "base64",
                "content": base64.b64encode(encoded[path]).decode(),
            }
        raise AssertionError(url)

    certificate = _certificate(envelope)
    completed = subprocess.CompletedProcess(
        args=(),
        returncode=0,
        stdout=json.dumps([{
            "verificationResult": {
                "signature": {"certificate": certificate},
                "statement": {
                    "subject": [{"digest": {"sha256": envelope["subject_sha256"]}}],
                    "predicateType": "https://slsa.dev/provenance/v1",
                },
                "verifiedTimestamps": [{"type": "rekor"}],
            },
        }]),
        stderr="",
    )
    commands: list[tuple[str, ...]] = []

    def command_runner(argv: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(argv))  # type: ignore[arg-type]
        return completed

    verifier = GitHubAttestationVerifier(
        policy,
        json_reader=json_reader,
        command_runner=command_runner,
    )

    assert verifier.verify(
        envelope, plan, str(receipt["run_id"]), int(receipt["attempt"]),
    ) == receipt
    assert ("--hostname", "github.com") == commands[0][
        commands[0].index("--hostname"):commands[0].index("--hostname") + 2
    ]


def test_pinned_workflow_digest_rejects_remote_policy_downgrade(tmp_path: Path) -> None:
    repository_policy, repository_policy_bytes = _repository_policy()
    workflow = f"uses: actions/attest-build-provenance@{ACTION_SHA}\n".encode()
    policy = _policy(
        tmp_path, workflow, b"runner", str(repository_policy["policy_fingerprint"]),
    )
    envelope = GitHubOIDCAttestationEnvelope.from_dict(runner.build_envelope(
        repository_policy,
        {"candidate_sha": CANDIDATE_SHA, "candidate_tree": CANDIDATE_TREE},
        run_id="123",
        attempt=1,
    ))

    def json_reader(url: str) -> dict[str, object]:
        if "/actions/runs/" in url:
            return {
                "id": 123,
                "run_attempt": 1,
                "head_sha": WORKFLOW_SHA,
                "head_branch": "main",
                "path": policy.workflow_path,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": policy.repository},
            }
        if "/git/commits/" in url:
            return {"sha": CANDIDATE_SHA, "tree": {"sha": CANDIDATE_TREE}}
        marker = "/contents/"
        path = unquote(url.split(marker, 1)[1].split("?", 1)[0])
        contents = {
            policy.workflow_path: workflow + b"downgrade",
            policy.runner_path: b"runner",
            policy.attestor_policy_path: repository_policy_bytes,
        }
        return {"encoding": "base64", "content": base64.b64encode(contents[path]).decode()}

    verifier = GitHubAttestationVerifier(policy, json_reader=json_reader)

    with pytest.raises(VerificationProviderError) as failure:
        verifier._require_live_github(
            envelope, WORKFLOW_SHA,
        )
    assert failure.value.code == "policy_downgrade"
