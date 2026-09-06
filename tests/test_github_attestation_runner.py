from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from subprocess import CompletedProcess

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


def test_isolated_commands_receive_fresh_candidate_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    copies = []
    users = []
    path_preparations = []

    monkeypatch.setattr(runner, "_verify_canonical_checkout", lambda *_args: None)
    def create_user(_prefix: str, index: int) -> tuple[str, tuple[str, str, str]]:
        identity = (f"phase4candidate{index}", (str(998 + index), "999", "candidate"))
        users.append(identity)
        return identity

    monkeypatch.setattr(runner, "_create_candidate_user", create_user)
    def prepare_path(*_args: object, **_kwargs: object) -> str:
        path_preparations.append(len(copies))
        return os.environ.get("PATH", "")

    monkeypatch.setattr(runner, "_candidate_path", prepare_path)
    monkeypatch.setattr(runner, "_verify_candidate_path", lambda *_args: None)

    def fresh(*_args: object) -> tuple[Path, Path]:
        temporary = tmp_path / f"copy-{len(copies)}"
        work = temporary / "work"
        work.mkdir(parents=True)
        copies.append(work)
        return temporary, work

    def execute(
        command: list[str], *, cwd: Path, **_kwargs: object,
    ) -> CompletedProcess[bytes]:
        assert command[:3] == ["/usr/bin/sudo", "--non-interactive", "--user"]
        if len(copies) == 1:
            (cwd / "poisoned").write_text("yes", encoding="utf-8")
        else:
            assert not (cwd / "poisoned").exists()
        return CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr(runner, "_fresh_candidate_copy", fresh)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    monkeypatch.setattr(runner, "_terminate_candidate", lambda _uid: None)
    monkeypatch.setattr(runner, "_remove_candidate_copy", lambda _path: None)
    monkeypatch.setattr(runner, "_delete_candidate_user", lambda *_args: None)
    contract = [
        {"suite_id": f"suite::{index}", "suite_type": "unit", "argv": ["tool"],
         "timeout_seconds": 10, "cwd": ".", "artifacts": []}
        for index in (1, 2)
    ]

    results, *_ = runner._execute_commands(
        contract, candidate, candidate_sha=SHA, candidate_user="phase4candidate",
    )

    assert [result["status"] for result in results] == ["PASS", "PASS"]
    assert copies[0] != copies[1]
    assert path_preparations == [0]
    assert users[0][0] != users[1][0]
    assert users[0][1][0] != users[1][1][0]


def test_materialize_tree_copies_exact_blob_bytes_without_archive_transforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    destination = tmp_path / "destination"
    candidate.mkdir()
    destination.mkdir()
    content = b"$Format:%H$\n"
    (candidate / "module.py").write_bytes(content)
    blob = runner._git_blob_sha(content)

    monkeypatch.setattr(
        runner, "_run_checked",
        lambda *_args, **_kwargs: CompletedProcess(
            [], 0, f"100644 blob {blob}\tmodule.py\0".encode(), b"",
        ),
    )

    runner._materialize_tree(candidate, SHA, destination)

    assert (destination / "module.py").read_bytes() == content


@pytest.mark.parametrize(
    ("record", "content"),
    [
        (f"100644 blob {'0' * 40}\tmodule.py\0".encode(), b"different\n"),
        (f"120000 blob {'0' * 40}\tmodule.py\0".encode(), b"target\n"),
        (f"160000 commit {'0' * 40}\tsubmodule\0".encode(), b""),
    ],
)
def test_materialize_tree_rejects_blob_mismatch_links_and_gitlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: bytes, content: bytes,
) -> None:
    candidate = tmp_path / "candidate"
    destination = tmp_path / "destination"
    candidate.mkdir()
    destination.mkdir()
    (candidate / "module.py").write_bytes(content)
    monkeypatch.setattr(
        runner, "_run_checked",
        lambda *_args, **_kwargs: CompletedProcess([], 0, record, b""),
    )

    with pytest.raises(ValueError):
        runner._materialize_tree(candidate, SHA, destination)


def test_isolated_command_fails_closed_when_candidate_process_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    work = tmp_path / "copy" / "work"
    candidate.mkdir()
    work.mkdir(parents=True)
    monkeypatch.setattr(runner, "_verify_canonical_checkout", lambda *_args: None)
    monkeypatch.setattr(
        runner, "_create_candidate_user",
        lambda _prefix, _index: ("phase4candidate1", ("999", "999", "candidate")),
    )
    monkeypatch.setattr(
        runner, "_candidate_path", lambda *_args, **_kwargs: os.environ.get("PATH", ""),
    )
    monkeypatch.setattr(runner, "_verify_candidate_path", lambda *_args: None)
    monkeypatch.setattr(
        runner, "_fresh_candidate_copy", lambda *_args: (work.parent, work),
    )
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda command, **_kwargs: CompletedProcess(command, 0, b"", b""),
    )
    monkeypatch.setattr(
        runner, "_terminate_candidate", lambda _uid: "candidate_process_survived",
    )
    monkeypatch.setattr(runner, "_remove_candidate_copy", lambda _path: None)
    monkeypatch.setattr(runner, "_delete_candidate_user", lambda *_args: None)
    contract = [{
        "suite_id": "suite::1", "suite_type": "unit", "argv": ["tool"],
        "timeout_seconds": 10, "cwd": ".", "artifacts": [],
    }]

    results, *_ = runner._execute_commands(
        contract, candidate, candidate_sha=SHA, candidate_user="phase4candidate",
    )

    assert results[0]["status"] == "ERROR"
    assert results[0]["error_code"] == "candidate_process_survived"


def test_candidate_path_drops_missing_entries_and_keeps_verified_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "tools"
    existing.mkdir()
    tool = existing / "tool"
    tool.write_bytes(b"tool")
    tool.chmod(0o755)
    (tmp_path / "trusted-bin").mkdir(mode=0o700)
    missing = tmp_path / "future-tools"
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda command, **_kwargs: CompletedProcess(
            command, 1 if command[5] == "/usr/bin/test" else 0, b"", b"",
        ),
    )
    monkeypatch.setattr(
        runner, "_run_checked", lambda command, **_kwargs: CompletedProcess(command, 0, b"", b""),
    )

    result = runner._candidate_path(
        "phase4candidate1", os.pathsep.join((str(existing), str(missing))),
        command_names=("tool",), trusted_root=tmp_path,
    )

    assert result == os.pathsep.join((str(tmp_path / "trusted-bin"), str(existing)))


def test_candidate_path_freezes_command_from_writable_runner_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    writable = tmp_path / "runner-tools"
    system = tmp_path / "system-tools"
    temporary = tmp_path / "command"
    writable.mkdir()
    system.mkdir()
    temporary.mkdir()
    tool = writable / "uv"
    tool.write_bytes(b"trusted executable bytes")
    tool.chmod(0o755)

    def access_check(command: list[str], **_kwargs: object) -> CompletedProcess[bytes]:
        if command[5] == "/usr/bin/test":
            return CompletedProcess(
                command, 0 if command[-1] == str(writable) else 1, b"", b"",
            )
        return CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(runner.subprocess, "run", access_check)
    def checked(command: list[str], **_kwargs: object) -> CompletedProcess[bytes]:
        if command[:4] == [
            "/usr/bin/sudo", "--non-interactive", "/bin/chown", "--recursive",
        ]:
            trusted_bin = temporary / "trusted-bin"
            assert (trusted_bin.stat().st_mode & 0o222) == 0
            assert all((item.stat().st_mode & 0o222) == 0 for item in trusted_bin.iterdir())
        return CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(runner, "_run_checked", checked)

    result = runner._candidate_path(
        "phase4candidate1", os.pathsep.join((str(writable), str(system))),
        command_names=("uv",), trusted_root=temporary,
    )

    trusted_bin = temporary / "trusted-bin"
    assert result == os.pathsep.join((str(trusted_bin), str(system)))
    assert (trusted_bin / "uv").read_bytes() == b"trusted executable bytes"
    assert (trusted_bin.stat().st_mode & 0o222) == 0
    assert ((trusted_bin / "uv").stat().st_mode & 0o222) == 0


def test_candidate_path_excludes_readonly_directory_with_writable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "uv"
    tool.write_bytes(b"mutable")
    tool.chmod(0o755)

    def access_check(command: list[str], **_kwargs: object) -> CompletedProcess[bytes]:
        if command[5] == "/usr/bin/test":
            return CompletedProcess(command, 1, b"", b"")
        return CompletedProcess(command, 0, str(tools / "uv").encode(), b"")

    monkeypatch.setattr(runner.subprocess, "run", access_check)
    monkeypatch.setattr(
        runner, "_run_checked", lambda command, **_kwargs: CompletedProcess(command, 0, b"", b""),
    )

    result = runner._candidate_path(
        "phase4candidate1", str(tools),
        command_names=("uv",), trusted_root=tmp_path,
    )

    assert result == str(tmp_path / "trusted-bin")


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
    assert "sudo useradd" not in workflow
    assert "sudo chown -R phase4candidate:phase4candidate candidate" not in workflow
    assert "chmod 700 phase4-output" in workflow
    assert "--candidate-user phase4candidate" in workflow
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
    assert "git archive" not in runner_text
    assert '"ls-tree", "--recursive"' in runner_text
    assert "_create_candidate_user" in runner_text
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
