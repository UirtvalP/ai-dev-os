"""验证适配层合同回归；注入端口仅证明协议，真实 LPAC 用单独 Windows 测试。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from workspace_orchestrator.integration.verification import (
    CommandExecution,
    LegacyVerificationAdapter,
    LegacyVerificationError,
    WindowsIsolatedCommandPort,
)
from workspace_orchestrator.orchestration.contracts import (
    PolicyError,
    VerificationCommand,
    VerificationPlan,
    commands_fingerprint,
    fingerprint,
)
from workspace_orchestrator.orchestration.isolation import (
    WindowsAppContainerIsolation,
    WorkerIsolationError,
    stage_python_runtime,
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, check=True,
    )
    return completed.stdout.decode("utf-8").strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "verification fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    (root / "source.txt").write_bytes(b"candidate\x00\xff\r\n")
    _git(root, "add", "source.txt")
    _git(root, "commit", "-m", "candidate")
    return root


class FixtureCommandPort:
    """受控纯协议 fixture，不宣称进程隔离能力或生产 PASS 证据。"""

    def __init__(self) -> None:
        self.calls = []
        self.actions = []
        self.actual_environment = {"isolation_backend": "trusted-test-fixture"}

    def environment(self):
        return dict(self.actual_environment)

    def run(self, command, *, snapshot_path, protected_roots, run_id):
        self.calls.append((command, snapshot_path, protected_roots, run_id))
        assert not (snapshot_path / ".git").exists()
        assert snapshot_path.name == "candidate"
        if self.actions:
            action = self.actions.pop(0)
            if callable(action):
                return action(command, snapshot_path)
            if isinstance(action, BaseException):
                raise action
            return action
        return _execution()


def _execution(*, returncode=0, cleanup=True, stdout=b"PASS\x00\xff\r\n", stderr=b"warning\n"):
    return CommandExecution(
        returncode, hashlib.sha256(stdout).hexdigest(), hashlib.sha256(stderr).hexdigest(),
        0.125, cleanup, {"fixture_only": True, "actual_argv": ["fixture"]},
    )


def _adapter(repository, tmp_path, port):
    protected = tmp_path / "protected"
    protected.mkdir(exist_ok=True)
    (protected / "gate.json").write_text('{"trusted": true}', encoding="utf-8")
    return LegacyVerificationAdapter(protected_roots=(protected,), command_port=port)


def _plan(repository, environment, commands=None):
    commands = commands or (VerificationCommand("unit", ("{python}", "-c", "print('ok')")),)
    return VerificationPlan(
        "verification-plan", "REQ-001", "task", _git(repository, "rev-parse", "HEAD"),
        _git(repository, "rev-parse", "HEAD^{tree}"), dict(environment), commands,
        commands_fingerprint(commands),
    )


def _receipt_diagnostics(receipt):
    """失败时显示有界原始输出，准确 hash 断言仍覆盖完整未过滤字节流。"""
    details = [
        {
            "result": result.to_dict(),
            "argv": evidence.get("actual_argv"),
            "stdout_preview_hex": evidence.get("stdout_preview_hex", ""),
            "stderr_preview_hex": evidence.get("stderr_preview_hex", ""),
            "stdout_suffix_preview_hex": evidence.get("stdout_suffix_preview_hex", ""),
            "stderr_suffix_preview_hex": evidence.get("stderr_suffix_preview_hex", ""),
            "stdout_bytes": evidence.get("stdout_bytes"),
            "stderr_bytes": evidence.get("stderr_bytes"),
        }
        for result, evidence in zip(receipt.results, receipt.extra["execution_evidence"], strict=True)
    ]
    # 显式输出多行 JSON，避免 pytest 把 assertion 的 list/bytes repr 缩成省略号。
    rendered = json.dumps(details, ensure_ascii=True, indent=2)
    print("VERIFICATION_RECEIPT_DIAGNOSTIC\n" + rendered, flush=True)
    return rendered


def test_output_digest_short_preview_is_complete_without_duplicate_suffix():
    from workspace_orchestrator.integration.verification import _OutputDigest

    output = b"short raw output\x00\xff\r\n"
    capture = _OutputDigest(io.BytesIO(output))
    capture.drain()

    assert bytes(capture.preview) == output
    assert capture.suffix_preview == b""
    assert capture.preview_complete is True
    assert capture.size == len(output)
    assert capture.digest.hexdigest() == hashlib.sha256(output).hexdigest()


def test_output_digest_long_preview_preserves_prefix_hash_and_final_exception_tail():
    from workspace_orchestrator.integration.verification import _OutputDigest

    prefix = bytes(range(256)) * 16
    final = b"PermissionError: final hosted exception\r\n"
    output = prefix + b"middle" * 1000 + final
    capture = _OutputDigest(io.BytesIO(output))
    capture.drain()

    assert bytes(capture.preview) == prefix
    assert capture.suffix_preview == output[-4096:]
    assert final in capture.suffix_preview
    assert capture.preview_complete is False
    assert capture.size == len(output)
    assert capture.digest.hexdigest() == hashlib.sha256(output).hexdigest()


def test_legacy_receipt_binds_actual_candidate_commands_environment_and_raw_outputs(repository, tmp_path):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    plan = _plan(repository, adapter.environment)

    def inspect_copy(command, snapshot):
        assert (snapshot / "source.txt").read_bytes() == b"candidate\x00\xff\r\n"
        (snapshot / "build-output.txt").write_text("changes stay in test copy", encoding="utf-8")
        return _execution()

    port.actions.append(inspect_copy)
    receipt = adapter.execute(plan, workspace_path=repository)
    receipt.validate_for(plan)
    assert receipt.provider_id == "legacy-verification-adapter"
    assert receipt.provider_version == "1"
    assert receipt.results[0].stdout_sha256 == hashlib.sha256(b"PASS\x00\xff\r\n").hexdigest()
    assert receipt.results[0].stderr_sha256 == hashlib.sha256(b"warning\n").hexdigest()
    assert receipt.extra["commands"] == [command.to_dict() for command in plan.commands]
    assert receipt.extra["snapshot_kind"] == "git-tracked-blobs-without-shared-git"
    assert receipt.extra["snapshot_cleaned"] is True
    assert not port.calls[0][1].exists()
    assert repository in port.calls[0][2]
    assert repository / ".git" in port.calls[0][2]
    assert (repository / "source.txt").read_bytes() == b"candidate\x00\xff\r\n"


def test_nonzero_result_does_not_parse_markdown_pass_or_skip_remaining_commands(repository, tmp_path):
    port = FixtureCommandPort()
    port.actions = [_execution(returncode=7, stdout=b"# PASS\nall good"), _execution()]
    adapter = _adapter(repository, tmp_path, port)
    commands = (VerificationCommand("first", ("fixture",)), VerificationCommand("second", ("fixture",)))
    plan = _plan(repository, adapter.environment, commands)
    receipt = adapter.execute(plan, workspace_path=repository)
    assert [item.returncode for item in receipt.results] == [7, 0]
    assert len(port.calls) == 2
    with pytest.raises(PolicyError) as caught:
        receipt.validate_for(plan)
    assert caught.value.code == "verification_failed"


def test_timeout_fact_is_a_failed_receipt_not_pass(repository, tmp_path):
    port = FixtureCommandPort()
    port.actions = [replace(_execution(returncode=124), evidence={"timed_out": True})]
    adapter = _adapter(repository, tmp_path, port)
    plan = _plan(repository, adapter.environment)
    receipt = adapter.execute(plan, workspace_path=repository)
    assert receipt.extra["execution_evidence"][0]["timed_out"] is True
    with pytest.raises(PolicyError, match="未全部成功"):
        receipt.validate_for(plan)


@pytest.mark.parametrize("error", [OSError("failed launch"), RuntimeError("unknown execution")])
def test_unexpected_execution_exception_preserves_diagnostics_without_receipt(repository, tmp_path, error):
    port = FixtureCommandPort()
    port.actions = [error]
    adapter = _adapter(repository, tmp_path, port)
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(_plan(repository, adapter.environment), workspace_path=repository)
    assert caught.value.code == "execution_unknown"
    assert Path(caught.value.details["snapshot_path"]).is_dir()
    assert caught.value.details["completed_results"] == []


@pytest.mark.parametrize("field", ["candidate_sha", "candidate_tree"])
def test_stale_candidate_plan_is_rejected_before_execution(repository, tmp_path, field):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    plan = replace(_plan(repository, adapter.environment), **{field: "a" * 40})
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(plan, workspace_path=repository)
    assert caught.value.code == "stale_verification"
    assert not port.calls


def test_dirty_candidate_is_rejected_before_execution(repository, tmp_path):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    plan = _plan(repository, adapter.environment)
    (repository / "untracked").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(plan, workspace_path=repository)
    assert caught.value.code == "dirty_candidate"
    assert not port.calls


@pytest.mark.parametrize("commit", [False, True])
def test_candidate_changes_during_execution_invalidate_receipt(repository, tmp_path, commit):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    plan = _plan(repository, adapter.environment)

    def drift(command, snapshot):
        (repository / "new.txt").write_text("concurrent controller change", encoding="utf-8")
        if commit:
            _git(repository, "add", "new.txt")
            _git(repository, "commit", "-m", "drift")
        return _execution()

    port.actions = [drift]
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(plan, workspace_path=repository)
    assert caught.value.code == ("stale_verification" if commit else "dirty_candidate")
    assert len(caught.value.details["completed_results"]) == 1


@pytest.mark.parametrize("change", ["modify", "remove"])
def test_snapshot_tracked_changes_cannot_be_bound_to_original_tree(repository, tmp_path, change):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    plan = _plan(repository, adapter.environment)

    def alter_snapshot(command, snapshot):
        target = snapshot / "source.txt"
        if change == "modify":
            target.write_bytes(b"different source under test")
        else:
            target.unlink()
        return _execution()

    port.actions = [alter_snapshot]
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(plan, workspace_path=repository)
    assert caught.value.code == "snapshot_changed"
    assert (repository / "source.txt").read_bytes() == b"candidate\x00\xff\r\n"


def test_command_hash_cannot_be_reused_for_different_argv(repository, tmp_path):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    plan = _plan(repository, adapter.environment)
    with pytest.raises(PolicyError, match="命令指纹"):
        replace(plan, commands=(VerificationCommand("unit", ("different-program",)),))
    assert not port.calls


@pytest.mark.parametrize("stage", ["constructor", "plan", "before", "during"])
def test_actual_environment_must_match_fixed_plan(repository, tmp_path, stage):
    port = FixtureCommandPort()
    protected = tmp_path / "protected"
    protected.mkdir()
    if stage == "constructor":
        with pytest.raises(LegacyVerificationError) as caught:
            LegacyVerificationAdapter(
                protected_roots=(protected,), environment={"platform": "invented"}, command_port=port,
            )
    else:
        adapter = _adapter(repository, tmp_path, port)
        plan = _plan(repository, adapter.environment)
        if stage == "plan":
            plan = replace(plan, environment={"platform": "invented"})
        elif stage == "before":
            port.actual_environment["changed"] = "yes"
        else:
            def drift(command, snapshot):
                port.actual_environment["changed"] = "yes"
                return _execution()
            port.actions = [drift]
        with pytest.raises(LegacyVerificationError) as caught:
            adapter.execute(plan, workspace_path=repository)
    assert caught.value.code == "environment_mismatch"


def test_unconfirmed_process_cleanup_never_yields_receipt(repository, tmp_path):
    port = FixtureCommandPort()
    port.actions = [_execution(cleanup=False)]
    adapter = _adapter(repository, tmp_path, port)
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(_plan(repository, adapter.environment), workspace_path=repository)
    assert caught.value.code == "cleanup_unconfirmed"
    assert Path(caught.value.details["snapshot_path"]).is_dir()


def test_snapshot_cleanup_failure_never_yields_receipt(repository, tmp_path, monkeypatch):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)

    def fail_cleanup(temporary_root, snapshot):
        raise OSError("snapshot still retained")

    monkeypatch.setattr(adapter, "_cleanup_snapshot", fail_cleanup)
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(_plan(repository, adapter.environment), workspace_path=repository)
    assert Path(caught.value.details["snapshot_path"]).is_dir()
    assert len(caught.value.details["completed_results"]) == 1


def test_default_backend_fails_closed_on_unsupported_platform(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from workspace_orchestrator.integration import verification

    monkeypatch.setattr(verification, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(LegacyVerificationError) as caught:
        WindowsIsolatedCommandPort().run(
            VerificationCommand("unit", ("{python}", "-c", "pass")), snapshot_path=tmp_path,
            protected_roots=(tmp_path,), run_id="verification-fixture",
        )
    assert caught.value.code == "isolation_unavailable"


def test_exact_legacy_git_check_uses_trusted_native_git_not_candidate_port(repository, tmp_path):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    command = VerificationCommand("whitespace", ("git", "diff", "--check"))
    plan = _plan(repository, adapter.environment, (command,))
    receipt = adapter.execute(plan, workspace_path=repository)
    receipt.validate_for(plan)
    assert not port.calls
    evidence = receipt.extra["execution_evidence"][0]
    assert evidence["execution_boundary"] == "trusted-git-static-candidate-diff"
    assert "diff-tree" in evidence["actual_argv"]
    assert evidence["candidate_sha"] == plan.candidate_sha
    assert evidence["private_candidate_commit"] in evidence["actual_argv"]
    assert Path(evidence["working_directory"]).name == "candidate"
    assert not any(str(repository) in argument for argument in evidence["actual_argv"])
    assert any(argument.startswith("--git-dir=") for argument in evidence["actual_argv"])
    assert any(
        argument == f"--work-tree={evidence['working_directory']}"
        for argument in evidence["actual_argv"]
    )
    assert evidence["actual_argv_fingerprint"] == fingerprint(evidence["actual_argv"])
    assert evidence["stdout_suffix_preview_hex"] == ""
    assert evidence["stderr_suffix_preview_hex"] == ""
    assert evidence["stdout_preview_complete"] is True
    assert evidence["stderr_preview_complete"] is True
    json.dumps(receipt.to_dict(), ensure_ascii=True)


@pytest.mark.parametrize("state", ["marker", "git-dir", "both"])
def test_private_git_cleanup_is_partial_state_safe_and_idempotent(tmp_path, state):
    snapshot = tmp_path / "candidate"
    snapshot.mkdir()
    marker = snapshot / ".git"
    git_dir = tmp_path / "git-static-fixture"
    if state in {"marker", "both"}:
        marker.write_text("gitdir: fixture", encoding="utf-8")
    if state in {"git-dir", "both"}:
        objects = git_dir / "objects"
        objects.mkdir(parents=True)
        item = objects / "object"
        item.write_bytes(b"object")
        item.chmod(stat.S_IREAD)

    LegacyVerificationAdapter._remove_private_git(snapshot, marker, git_dir)
    LegacyVerificationAdapter._remove_private_git(snapshot, marker, git_dir)

    assert not marker.exists()
    assert not git_dir.exists()


def test_private_git_cleanup_never_accepts_target_outside_verification_root(tmp_path):
    snapshot = tmp_path / "verification" / "candidate"
    snapshot.mkdir(parents=True)
    marker = snapshot / ".git"
    outside = tmp_path / "git-static-outside"
    marker.write_text("private", encoding="utf-8")
    outside.mkdir()

    with pytest.raises(LegacyVerificationError) as caught:
        LegacyVerificationAdapter._remove_private_git(snapshot, marker, outside)

    assert caught.value.code == "cleanup_unconfirmed"
    assert marker.exists() and outside.exists()


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_private_git_init_failure_cleans_whichever_partial_target_exists(
    tmp_path, monkeypatch, failure,
):
    from types import SimpleNamespace

    from workspace_orchestrator.integration import verification as verification_module

    snapshot = tmp_path / "candidate"
    snapshot.mkdir()

    def fail_init(command, **_kwargs):
        git_dir = Path(next(item for item in command if item.startswith("--separate-git-dir="))[19:])
        if failure == "timeout":
            git_dir.mkdir()
            raise subprocess.TimeoutExpired(command, 30)
        (snapshot / ".git").write_text("partial marker", encoding="utf-8")
        return subprocess.CompletedProcess(command, 3, b"", b"init rejected")

    monkeypatch.setattr(verification_module.subprocess, "run", fail_init)
    with pytest.raises(LegacyVerificationError) as caught:
        LegacyVerificationAdapter._private_git_candidate(
            snapshot, SimpleNamespace(executable="git"), object_format="sha1",
        )

    assert caught.value.code == "git_static_check_unavailable"
    assert not (snapshot / ".git").exists()
    assert not list(tmp_path.glob("git-static-*"))


def test_private_git_constructor_failure_preserves_original_and_cleans_both_targets(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from workspace_orchestrator.integration import verification as verification_module

    snapshot = tmp_path / "candidate"
    snapshot.mkdir()

    def initialized(command, **_kwargs):
        git_dir = Path(next(item for item in command if item.startswith("--separate-git-dir="))[19:])
        git_dir.mkdir()
        (git_dir / "config").write_text("private", encoding="utf-8")
        (snapshot / ".git").write_text("private marker", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(verification_module.subprocess, "run", initialized)
    monkeypatch.setattr(
        verification_module, "TrustedGit",
        lambda _snapshot: (_ for _ in ()).throw(RuntimeError("constructor failed")),
    )
    with pytest.raises(RuntimeError, match="constructor failed"):
        LegacyVerificationAdapter._private_git_candidate(
            snapshot, SimpleNamespace(executable="git"), object_format="sha1",
        )

    assert not (snapshot / ".git").exists()
    assert not list(tmp_path.glob("git-static-*"))


def test_private_git_cleanup_attempts_git_dir_after_marker_unlink_failure_and_keeps_original(
    tmp_path, monkeypatch,
):
    snapshot = tmp_path / "candidate"
    snapshot.mkdir()
    marker = snapshot / ".git"
    marker.write_text("private", encoding="utf-8")
    git_dir = tmp_path / "git-static-fixture"
    git_dir.mkdir()
    (git_dir / "object").write_bytes(b"object")
    unlink = Path.unlink

    def fail_marker(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("marker locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker)
    original = RuntimeError("static check failed")
    with pytest.raises(LegacyVerificationError) as caught:
        LegacyVerificationAdapter._finish_private_git_cleanup(
            snapshot, marker, git_dir, original=original,
        )

    assert caught.value.code == "cleanup_unconfirmed"
    assert caught.value.details["original_failure"]["message"] == "static check failed"
    assert caught.value.__cause__ is original
    assert marker.exists()
    assert not git_dir.exists()


def test_private_git_cleanup_midway_failure_is_fail_closed(tmp_path, monkeypatch):
    snapshot = tmp_path / "candidate"
    snapshot.mkdir()
    marker = snapshot / ".git"
    marker.write_text("private", encoding="utf-8")
    git_dir = tmp_path / "git-static-fixture"
    git_dir.mkdir()
    item = git_dir / "object"
    item.write_bytes(b"object")
    chmod = Path.chmod

    def fail_object(path, mode, *args, **kwargs):
        if path == item:
            raise PermissionError("object locked")
        return chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", fail_object)
    with pytest.raises(LegacyVerificationError) as caught:
        LegacyVerificationAdapter._remove_private_git(snapshot, marker, git_dir)

    assert caught.value.code == "cleanup_unconfirmed"
    assert any(
        failure.startswith("git_file_chmod:")
        for failure in caught.value.details["cleanup_failures"]
    )
    assert not marker.exists()


def test_legacy_git_check_preserves_actual_nonzero_and_output_hash(repository, tmp_path):
    from workspace_orchestrator.integration.git_workspace import TrustedGit

    (repository / "whitespace.txt").write_bytes(b"bad trailing whitespace \n")
    _git(repository, "add", "whitespace.txt")
    _git(repository, "commit", "-m", "whitespace candidate")
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    command = VerificationCommand("whitespace", ("git", "diff", "--check"))
    plan = _plan(repository, adapter.environment, (command,))
    receipt = adapter.execute(plan, workspace_path=repository)
    actual = TrustedGit(repository).checked_diff(plan.candidate_sha)
    assert actual.returncode != 0
    assert receipt.results[0].returncode == actual.returncode
    assert receipt.results[0].stdout_sha256 == hashlib.sha256(actual.stdout).hexdigest()
    assert receipt.results[0].stderr_sha256 == hashlib.sha256(actual.stderr).hexdigest()
    with pytest.raises(PolicyError, match="未全部成功"):
        receipt.validate_for(plan)
    assert not port.calls


def test_git_static_translation_does_not_accept_additional_operator_argv(repository, tmp_path):
    port = FixtureCommandPort()
    adapter = _adapter(repository, tmp_path, port)
    command = VerificationCommand("not-static", ("git", "diff", "--check", "--output=elsewhere"))
    adapter.execute(_plan(repository, adapter.environment, (command,)), workspace_path=repository)
    assert port.calls[0][0] == command  # 只有可信注入 fixture 接受；真实端口拒绝相对可执行程序。


def test_dependency_environment_tracks_existing_bytes_but_never_executes_pth(tmp_path):
    dependencies = tmp_path / "site-packages"
    dependencies.mkdir()
    source = dependencies / "dependency.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (dependencies / "redirect.pth").write_text("import nonexistent_destructive_startup\n", encoding="utf-8")
    port = WindowsIsolatedCommandPort(python_dependencies=(dependencies,), python_scripts=())
    before = port.environment()
    assert "redirect.pth" not in " ".join(port._dependencies())
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert port.environment()["python_dependencies_sha256"] != before["python_dependencies_sha256"]


def test_dependency_links_are_not_copied_into_trusted_snapshot(tmp_path):
    import os

    dependencies = tmp_path / "site-packages"
    dependencies.mkdir()
    target = tmp_path / "external.py"
    target.write_text("private source", encoding="utf-8")
    try:
        os.symlink(target, dependencies / "alias.py")
    except OSError:
        pytest.skip("当前临时测试环境不支持创建符号链接")
    port = WindowsIsolatedCommandPort(python_dependencies=(dependencies,), python_scripts=())
    with pytest.raises(WorkerIsolationError):
        port.environment()


def test_source_digest_accepts_an_unchanged_real_exe_path(tmp_path):
    from workspace_orchestrator.integration.verification import _source_digest

    source = tmp_path / "ordinary.exe"
    source.write_bytes(b"ordinary EXE-named file, never executed")
    # Windows lstat/fstat 对该真实临时 EXE 的 executable 位可能不同；不是 fixture PASS 端口。
    assert _source_digest(source) == hashlib.sha256(source.read_bytes()).hexdigest()


def test_source_digest_accepts_actual_python_executable_identity():
    from workspace_orchestrator.integration.verification import _source_digest

    executable = Path(sys.executable).resolve(strict=True)
    assert _source_digest(executable) == hashlib.sha256(executable.read_bytes()).hexdigest()


def test_source_digest_normalizes_only_windows_cross_interface_executable_bits(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from workspace_orchestrator.integration import verification

    source = tmp_path / "native.exe"
    source.write_bytes(b"cross-interface mode representation")
    original = Path.lstat

    def path_mode(path):
        actual = original(path)
        if path != source:
            return actual
        values = {
            name: getattr(actual, name) for name in (
                "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
            )
        }
        values["st_mode"] |= 0o111
        values["st_file_attributes"] = getattr(actual, "st_file_attributes", 0)
        return SimpleNamespace(**values)

    monkeypatch.setattr(Path, "lstat", path_mode)
    if os.name == "nt":
        assert verification._source_digest(source) == hashlib.sha256(source.read_bytes()).hexdigest()
    else:
        with pytest.raises(LegacyVerificationError) as caught:
            verification._source_digest(source)
        assert caught.value.code == "environment_mismatch"


@pytest.mark.parametrize("field", ["st_mode", "st_ino", "st_size", "st_mtime_ns", "st_nlink"])
def test_source_digest_rejects_handle_identity_changes_during_read(tmp_path, monkeypatch, field):
    from types import SimpleNamespace

    from workspace_orchestrator.integration import verification

    source = tmp_path / "source.exe"
    source.write_bytes(b"unchanged content")
    original = verification.os.fstat
    calls = 0

    def changed(fd):
        nonlocal calls
        actual = original(fd)
        values = {
            name: getattr(actual, name) for name in (
                "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
            )
        }
        values["st_file_attributes"] = getattr(actual, "st_file_attributes", 0)
        calls += 1
        if calls == 2:
            values[field] = values[field] ^ stat.S_IWUSR if field == "st_mode" else values[field] + 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(verification.os, "fstat", changed)
    with pytest.raises(LegacyVerificationError) as caught:
        verification._source_digest(source)
    assert caught.value.code == "environment_mismatch"


def test_source_digest_rejects_actual_content_change_while_reading(tmp_path, monkeypatch):
    from workspace_orchestrator.integration import verification

    source = tmp_path / "changing.exe"
    source.write_bytes(b"before")
    original = verification.hashlib.file_digest

    def read_then_change(stream, algorithm):
        digest = original(stream, algorithm)
        source.write_bytes(b"different and longer source content")
        return digest

    monkeypatch.setattr(verification.hashlib, "file_digest", read_then_change)
    with pytest.raises(LegacyVerificationError) as caught:
        verification._source_digest(source)
    assert caught.value.code == "environment_mismatch"


@pytest.mark.parametrize("change", ["mode", "reparse"])
def test_source_digest_rejects_path_identity_change_even_when_handle_is_stable(tmp_path, monkeypatch, change):
    from types import SimpleNamespace

    from workspace_orchestrator.integration import verification

    source = tmp_path / "source.exe"
    source.write_bytes(b"stable handle")
    original_digest = verification.hashlib.file_digest
    original_lstat = Path.lstat
    consumed = False

    def mark_consumed(stream, algorithm):
        nonlocal consumed
        digest = original_digest(stream, algorithm)
        consumed = True
        return digest

    def changed_path(path):
        actual = original_lstat(path)
        if path != source or not consumed:
            return actual
        values = {
            name: getattr(actual, name) for name in (
                "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
            )
        }
        values["st_file_attributes"] = getattr(actual, "st_file_attributes", 0)
        if change == "mode":
            values["st_mode"] ^= stat.S_IWUSR
        else:
            values["st_file_attributes"] |= 0x400
        return SimpleNamespace(**values)

    monkeypatch.setattr(verification.hashlib, "file_digest", mark_consumed)
    monkeypatch.setattr(Path, "lstat", changed_path)
    with pytest.raises(LegacyVerificationError) as caught:
        verification._source_digest(source)
    assert caught.value.code == "environment_mismatch"


@pytest.fixture
def staging_fixture(tmp_path, monkeypatch):
    """纯内容/路径协议 fixture，不启动伪 Python，也不声称证明了 OS 隔离。"""
    from workspace_orchestrator.integration import verification

    sources = tmp_path / "trusted-sources"
    sources.mkdir()
    native = sources / "python.exe"
    native.write_bytes(b"fixture native executable, never executed")
    library = sources / "python3.dll"
    library.write_bytes(b"fixture native library, never loaded")
    standard = sources / "stdlib.py"
    standard.write_bytes(b"# fixture stdlib content\n")
    dependencies = sources / "dependencies"
    dependencies.mkdir()
    (dependencies / "dependency.py").write_bytes(b"VALUE = 1\n")
    tools = sources / "tools"
    tools.mkdir()
    tool = tools / "check.exe"
    tool.write_bytes(b"fixture native tool, never executed")
    domain = tmp_path / "private-domain"
    domain.mkdir()
    snapshot = domain / "candidate"
    snapshot.mkdir()
    (snapshot / "src").mkdir()
    port = WindowsIsolatedCommandPort(
        readonly_tools=(tools,), python_dependencies=(dependencies,), python_scripts=(),
    )

    def runtime_sources():
        return {
            name: (path, verification._source_digest(path))
            for name, path in {
                "controller-executable": native, "python.exe": native,
                "python3.dll": library, "stdlib/os.py": standard,
            }.items()
        }

    stage_calls = []

    def fake_stage(root):
        stage_calls.append(root)
        target = root / "python"
        target.mkdir()
        (target / "python.exe").write_bytes(native.read_bytes())
        (target / "python3.dll").write_bytes(library.read_bytes())
        version = f"{sys.version_info.major}{sys.version_info.minor}"
        with zipfile.ZipFile(target / f"python{version}.zip", "w") as bundle:
            bundle.writestr("os.py", standard.read_bytes())
        return target / "python.exe"

    monkeypatch.setattr(port, "_runtime_sources", runtime_sources)
    monkeypatch.setattr(verification, "stage_python_runtime", fake_stage)
    return {
        "port": port, "snapshot": snapshot, "native": native, "library": library,
        "standard": standard, "tool": tool, "dependencies": dependencies,
        "stage_calls": stage_calls,
    }


def _distribution_fixture(root, name, files, *, requires=(), entry_points=None):
    """仅生成标准安装元数据和惰性文件，不把它当成真实工具执行证明。"""
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    directory = root / f"{name.replace('-', '_')}-1.0.dist-info"
    directory.mkdir()
    (directory / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires),
        encoding="utf-8", newline="\n",
    )
    records = [*files, f"{directory.name}/METADATA", f"{directory.name}/RECORD"]
    if entry_points:
        (directory / "entry_points.txt").write_text(entry_points, encoding="utf-8", newline="\n")
        records.append(f"{directory.name}/entry_points.txt")
    (directory / "RECORD").write_text(
        "".join(f"{relative},,\n" for relative in records), encoding="utf-8", newline="\n",
    )


@pytest.fixture
def tool_staging_fixture(staging_fixture):
    from importlib import machinery

    item = staging_fixture
    dependencies = item["dependencies"]
    _distribution_fixture(dependencies, "pytest", {
        "pytest/__init__.py": b"# fixture\n", "pytest/__main__.py": b"# fixture\n",
        "_pytest/__init__.py": b"# fixture\n", "py.py": b"# fixture\n",
    }, requires=("pluggy>=1",))
    _distribution_fixture(dependencies, "pluggy", {"pluggy/__init__.py": b"# fixture\n"}, requires=("iniconfig",))
    _distribution_fixture(dependencies, "iniconfig", {"iniconfig/__init__.py": b"# fixture\n"})
    _distribution_fixture(dependencies, "pytest-xdist", {
        "xdist/__init__.py": b"# fixture\n", "xdist/plugin.py": b"# fixture\n",
    }, requires=("pytest", "execnet[peer]>=1"), entry_points="[pytest11]\nxdist = xdist.plugin\n")
    _distribution_fixture(dependencies, "execnet", {
        "execnet/__init__.py": b"# fixture\n",
    }, requires=('channel-peer; extra == "peer"',))
    _distribution_fixture(dependencies, "channel-peer", {"channel_peer.py": b"# fixture\n"})
    native_name = "08ae81f72d5a2b5fa9e0__mypyc"
    _distribution_fixture(dependencies, "mypy", {
        "mypy/__init__.py": b"# fixture\n", "mypy/__main__.py": b"# fixture\n",
        native_name + machinery.EXTENSION_SUFFIXES[0]: b"fixture native bytes, never loaded",
    }, requires=("typing-extensions",))
    _distribution_fixture(dependencies, "typing-extensions", {"typing_extensions.py": b"# fixture\n"})
    _distribution_fixture(dependencies, "ruff", {
        "ruff/__init__.py": b"# fixture\n", "ruff/__main__.py": b"# fixture\n",
    })
    _distribution_fixture(dependencies, "business", {"review_demo.py": b"VALUE = 42\n"})
    item["port"].environment()
    item["prepared"], _ = item["port"]._prepare(item["snapshot"])
    item["native_name"] = native_name
    return item


def test_trusted_tool_metadata_covers_internal_packages_plugins_and_transitive_extras(tool_staging_fixture):
    item = tool_staging_fixture
    identity = item["port"]._trusted_tool_identity(item["prepared"], "pytest")
    assert {"pytest", "_pytest", "py", "pluggy", "iniconfig", "xdist", "execnet", "channel_peer"} <= (
        set(identity["reserved_import_names"])
    )
    assert "review_demo" not in identity["reserved_import_names"]
    assert "pytest-xdist" in identity["distributions"]
    assert identity["entry_files"] == {
        "python/Lib/site-packages/pytest/__main__.py": item["prepared"].files["python/Lib/site-packages/pytest/__main__.py"],
    }
    mypy = item["port"]._trusted_tool_identity(item["prepared"], "mypy")
    assert item["native_name"] in mypy["reserved_import_names"]
    assert "typing_extensions" in mypy["reserved_import_names"]


@pytest.mark.parametrize(("module", "relative"), [
    ("pytest", "pytest.py"), ("pytest.__main__", "src/pytest/__init__.py"),
    ("pytest", "_pytest/config.py"), ("pytest", "src/pluggy/__init__.py"),
    ("pytest", "test_helpers/iniconfig.py"), ("pytest", "generated/xdist/plugin.py"),
    ("pytest", "src/execnet.py"), ("pytest", "generated/channel_peer.py"),
    ("pytest", "pytest.pyc"), ("pytest", "PyTeSt.PY"),
    ("ruff", "ruff.py"), ("mypy", "src/mypy/__main__.py"),
    ("mypy", "typing_extensions.py"), ("mypy", "08ae81f72d5a2b5fa9e0__mypyc.py"),
])
def test_trusted_tool_rejects_candidate_entry_and_internal_import_conflicts(tool_staging_fixture, module, relative):
    item = tool_staging_fixture
    collision = item["snapshot"] / relative
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_bytes(b"print('fake tool PASS')\n")
    with pytest.raises(LegacyVerificationError) as caught:
        item["port"]._trusted_tool_identity(item["prepared"], module)
    assert caught.value.code == "tool_import_conflict"


def test_trusted_tool_keeps_business_candidate_priority_without_reserving_all_installed_modules(tool_staging_fixture):
    item = tool_staging_fixture
    (item["snapshot"] / "src" / "review_demo.py").write_bytes(b"VALUE = 0\n")
    identity = item["port"]._trusted_tool_identity(item["prepared"], "pytest")
    assert identity["module"] == "pytest"
    assert "review_demo" not in identity["reserved_import_names"]


def test_trusted_tool_rechecks_generated_conflicts_before_the_next_command(tool_staging_fixture):
    item = tool_staging_fixture
    assert item["port"]._trusted_tool_identity(item["prepared"], "pytest")["module"] == "pytest"
    (item["snapshot"] / "pytest.py").write_bytes(b"print('generated forged tool')\n")
    with pytest.raises(LegacyVerificationError) as caught:
        item["port"]._trusted_tool_identity(item["prepared"], "pytest")
    assert caught.value.code == "tool_import_conflict"


@pytest.mark.parametrize("change", ["missing-dependency", "missing-record", "missing-entry"])
def test_trusted_tool_missing_installation_identity_never_falls_back_to_candidate(tool_staging_fixture, change):
    item = tool_staging_fixture
    prepared = item["prepared"]
    site = prepared.python.parent / "Lib" / "site-packages"
    if change == "missing-dependency":
        metadata_file = site / "pytest-1.0.dist-info" / "METADATA"
        with metadata_file.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("Requires-Dist: unavailable-tool-dependency\n")
    elif change == "missing-record":
        (site / "pytest-1.0.dist-info" / "RECORD").unlink()
    else:
        prepared = replace(prepared, files={
            name: digest for name, digest in prepared.files.items()
            if name != "python/Lib/site-packages/pytest/__main__.py"
        })
    with pytest.raises(LegacyVerificationError) as caught:
        item["port"]._trusted_tool_identity(prepared, "pytest")
    assert caught.value.code == "tool_environment_unavailable"


def test_packaging_is_lazy_and_missing_parser_does_not_break_non_tool_paths(tool_staging_fixture, monkeypatch):
    import builtins

    item = tool_staging_fixture
    original = builtins.__import__

    def no_packaging(name, *args, **kwargs):
        if name.startswith("packaging"):
            raise ImportError("fixture: no development parser installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_packaging)
    assert item["port"].environment()["python_tool_policy"]
    assert item["port"]._trusted_tool_identity(item["prepared"], None) == {}
    with pytest.raises(LegacyVerificationError) as caught:
        item["port"]._trusted_tool_identity(item["prepared"], "pytest")
    assert caught.value.code == "tool_environment_unavailable"


@pytest.mark.parametrize(("arguments", "expected"), [
    (("-m", "pytest", "-q"), "pytest"), (("-B", "-S", "-mpytest"), "pytest"),
    (("-X", "utf8", "-Wignore", "-m", "ruff"), "ruff"),
    (("-c", "print('ok')", "-m", "pytest"), None),
    (("script.py", "-m", "pytest"), None),
    (("-V", "-m", "pytest"), None),
])
def test_known_python_module_argv_cannot_be_hidden_by_supported_flags(arguments, expected):
    assert WindowsIsolatedCommandPort._python_module(arguments) == expected


@pytest.mark.parametrize("module", ["pytest", "pytest.__main__"])
def test_windows_lpac_pytest_launch_uses_honest_same_process_devnull_shim(tmp_path, module):
    from workspace_orchestrator.integration import verification

    task_root = tmp_path / "task"
    argv = (str(task_root / "python" / "python.exe"), "-B", "-m", module, "-q")

    transformed = WindowsIsolatedCommandPort._pytest_shim_launch(
        argv, task_root=task_root, run_id="verification-fixture-0",
    )

    assert transformed is not None
    launch_argv, devnull = transformed
    assert launch_argv == (
        argv[0], "-B", "-c", verification._PYTEST_DEVNULL_SHIM, module,
        str(Path(".ai-dev-os-worker") / "verification-fixture-0-e1" / "tmp"
            / ".ai-dev-os-pytest-devnull"), "1", "-q",
    )
    assert hashlib.sha256(launch_argv[3].encode()).hexdigest() == (
        verification._PYTEST_DEVNULL_SHIM_SHA256
    )
    assert devnull.relative_to(task_root).as_posix() == (
        ".ai-dev-os-worker/verification-fixture-0-e1/tmp/.ai-dev-os-pytest-devnull"
    )
    assert "subprocess" not in launch_argv[3]
    assert "runpy.run_module" in launch_argv[3]


def test_windows_lpac_non_pytest_module_does_not_use_devnull_shim(tmp_path):
    argv = (str(tmp_path / "python.exe"), "-B", "-m", "ruff", "check", ".")
    assert WindowsIsolatedCommandPort._pytest_shim_launch(
        argv, task_root=tmp_path, run_id="verification-fixture-0",
    ) is None


def test_pytest_devnull_shim_proves_controller_created_empty_physical_file(
    tmp_path, monkeypatch,
):
    import runpy

    from workspace_orchestrator.integration import verification

    task_root = tmp_path / "task"
    candidate = task_root / "candidate"
    candidate.mkdir(parents=True)
    relative = (
        Path(".ai-dev-os-worker") / "verification-fixture-0-e1" / "tmp"
        / ".ai-dev-os-pytest-devnull"
    )
    devnull = task_root / relative
    devnull.parent.mkdir(parents=True)
    devnull.write_bytes(b"")
    calls = []
    monkeypatch.chdir(candidate)
    monkeypatch.setattr(os, "devnull", os.devnull)
    launch_orig_argv = [
        str(task_root / "python" / "python.exe"), "-B", "-c",
        verification._PYTEST_DEVNULL_SHIM, "pytest", str(relative), "1", "-q",
    ]
    monkeypatch.setattr(sys, "orig_argv", launch_orig_argv)
    monkeypatch.setattr(sys, "argv", ["-c", "pytest", str(relative), "1", "-q"])
    monkeypatch.setattr(
        runpy, "run_module",
        lambda module, **options: calls.append((module, options, tuple(sys.argv))),
    )

    exec(  # noqa: S102 -- 定向执行产品内固定可信 shim，不执行候选或外部文本。
        verification._PYTEST_DEVNULL_SHIM, {"__name__": "pytest_devnull_shim_test"},
    )

    lexical = devnull.lstat()
    with devnull.open("rb") as stream:
        opened = os.fstat(stream.fileno())
    assert stat.S_ISREG(lexical.st_mode) and stat.S_ISREG(opened.st_mode)
    assert (lexical.st_dev, lexical.st_ino) == (opened.st_dev, opened.st_ino)
    assert lexical.st_nlink == opened.st_nlink == 1
    assert lexical.st_size == opened.st_size == 0
    assert os.devnull == str(devnull)
    assert calls == [("pytest", {"run_name": "__main__", "alter_sys": True}, ("pytest", "-q"))]
    assert sys.orig_argv == [launch_orig_argv[0], "-B", "-m", "pytest", "-q"]

@pytest.mark.skipif(sys.platform != "win32", reason="Windows devnull handle identity")
def test_pytest_private_devnull_controller_proof_is_physical_json_and_cleaned(tmp_path):
    task_root = tmp_path / "task"
    devnull = (
        task_root / ".ai-dev-os-worker" / "verification-fixture-0-e1" / "tmp"
        / ".ai-dev-os-pytest-devnull"
    )
    task_root.mkdir()
    held = WindowsIsolatedCommandPort._hold_pytest_devnull(
        devnull, task_root=task_root,
    )
    initial = WindowsIsolatedCommandPort._handle_file_state(held.handle, devnull)

    evidence = WindowsIsolatedCommandPort._cleanup_pytest_devnull(held)

    assert evidence == {
        "relative_task_path": (
            ".ai-dev-os-worker/verification-fixture-0-e1/tmp/.ai-dev-os-pytest-devnull"
        ),
        "volume_serial": initial["volume_serial"],
        "file_id": initial["file_id"],
        "object_type": "regular_file",
        "initial_size": 0,
        "initial_nlink": 1,
        "st_nlink": 1,
        "post_execution_size": 0,
        "delete_share_denied_during_execution": True,
        "cleaned": True,
    }
    assert evidence["file_id"] == held.file_id
    assert evidence["volume_serial"] == held.volume_serial
    json.dumps(evidence)
    assert not devnull.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows devnull delete-share lock")
def test_pytest_private_devnull_handle_blocks_same_name_replacement_until_cleanup(tmp_path):
    task_root = tmp_path / "task"
    task_root.mkdir()
    devnull = (
        task_root / ".ai-dev-os-worker" / "verification-fixture-0-e1" / "tmp"
        / ".ai-dev-os-pytest-devnull"
    )
    held = WindowsIsolatedCommandPort._hold_pytest_devnull(devnull, task_root=task_root)
    replacement = task_root / "replacement"
    replacement.write_bytes(b"attacker replacement")
    initial = WindowsIsolatedCommandPort._handle_file_state(held.handle, devnull)

    with pytest.raises(OSError):
        devnull.unlink()
    with pytest.raises(OSError):
        os.replace(replacement, devnull)

    current = WindowsIsolatedCommandPort._handle_file_state(held.handle, devnull)
    assert (current["volume_serial"], current["file_id"]) == (
        initial["volume_serial"], initial["file_id"],
    )
    evidence = WindowsIsolatedCommandPort._cleanup_pytest_devnull(held)
    assert evidence["file_id"] == initial["file_id"]
    assert evidence["cleaned"] is True
    assert not devnull.exists()


def test_pytest_private_devnull_rejects_linked_tmp_before_external_open_or_delete(
    tmp_path, monkeypatch,
):
    task_root = tmp_path / "task"
    run_root = task_root / ".ai-dev-os-worker" / "verification-fixture-0-e1"
    run_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / ".ai-dev-os-pytest-devnull"
    external.write_bytes(b"external sentinel")
    linked_tmp = run_root / "tmp"
    try:
        os.symlink(outside, linked_tmp, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("当前环境不能创建临时目录 symlink")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked_tmp), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if junction.returncode:
            pytest.skip("当前 Windows 不能创建临时目录 symlink/junction")
    lexical = linked_tmp / external.name
    opened, unlinked = [], []
    original_open, original_unlink = Path.open, Path.unlink

    def record_open(path, *args, **kwargs):
        if path in {lexical, external}:
            opened.append(path)
            raise AssertionError("祖先门禁前不得打开 task 外叶子")
        return original_open(path, *args, **kwargs)

    def record_unlink(path, *args, **kwargs):
        if path in {lexical, external}:
            unlinked.append(path)
            raise AssertionError("祖先门禁前不得删除 task 外叶子")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_open)
    monkeypatch.setattr(Path, "unlink", record_unlink)
    from workspace_orchestrator.integration import verification

    held = verification._HeldPytestDevnull(lexical, task_root, None, 0, "fixture", 0)
    with pytest.raises(LegacyVerificationError) as caught:
        WindowsIsolatedCommandPort._cleanup_pytest_devnull(held)

    assert caught.value.code == "cleanup_unconfirmed"
    assert opened == unlinked == []
    with original_open(external, "rb") as stream:
        assert stream.read() == b"external sentinel"


@pytest.mark.parametrize("drift", ["task_root", "tmp_ancestor"])
def test_pytest_private_devnull_physical_drift_is_rejected_before_leaf_access(
    tmp_path, monkeypatch, drift,
):
    from workspace_orchestrator.integration import verification

    task_root = tmp_path / "task"
    devnull = (
        task_root / ".ai-dev-os-worker" / "verification-fixture-0-e1" / "tmp"
        / ".ai-dev-os-pytest-devnull"
    )
    devnull.parent.mkdir(parents=True)
    devnull.write_bytes(b"")
    calls, leaf_access = [], []
    original_physical = verification._physical_path

    def reject_drift(path, *args, **kwargs):
        calls.append(path)
        if drift == "task_root" and path == task_root:
            return tmp_path / "replacement-task-root"
        if drift == "tmp_ancestor" and path == devnull:
            raise WorkerIsolationError("linked_path", "fixture ancestor drift")
        return original_physical(path, *args, **kwargs)

    def reject_leaf_access(path, *args, **kwargs):
        if path == devnull:
            leaf_access.append(path)
            raise AssertionError("物理祖先门禁前不得访问叶子内容")
        return Path.open(path, *args, **kwargs)

    monkeypatch.setattr(verification, "_physical_path", reject_drift)
    original_open = Path.open
    monkeypatch.setattr(
        Path, "open",
        lambda path, *args, **kwargs: (
            reject_leaf_access(path, *args, **kwargs)
            if path == devnull else original_open(path, *args, **kwargs)
        ),
    )
    held = verification._HeldPytestDevnull(devnull, task_root, None, 0, "fixture", 0)
    with pytest.raises(LegacyVerificationError) as caught:
        WindowsIsolatedCommandPort._cleanup_pytest_devnull(held)

    assert caught.value.code == "cleanup_unconfirmed"
    assert calls == ([task_root] if drift == "task_root" else [task_root, devnull])
    assert leaf_access == []
    assert devnull.exists()


def test_unknown_combined_python_option_cannot_bypass_tool_guard():
    with pytest.raises(LegacyVerificationError) as caught:
        WindowsIsolatedCommandPort._python_module(("-Bmpytest", "-q"))
    assert caught.value.code == "unsupported_python_argv"


@pytest.mark.parametrize("module", ["pytest", "pytest.__main__"])
def test_private_pytest_disables_debugging_after_requested_arguments(tool_staging_fixture, module):
    item = tool_staging_fixture
    command = VerificationCommand(
        "pytest", ("{python}", "-m", module, "-q", "-p", "debugging"), 60,
    )

    argv = item["port"]._command(command, item["prepared"])

    assert argv[-4:] == ("-p", "debugging", "-p", "no:debugging")


@pytest.mark.parametrize("module", ["pytest.config", "ruff.internal", "_pytest", "mypyc"])
def test_internal_test_tool_module_alias_requires_an_explicit_supported_entry(tool_staging_fixture, module):
    with pytest.raises(LegacyVerificationError) as caught:
        tool_staging_fixture["port"]._trusted_tool_identity(tool_staging_fixture["prepared"], module)
    assert caught.value.code == "unsupported_python_tool_entry"


def test_private_pth_keeps_trusted_stdlib_then_candidate_src_root_then_dependencies(staging_fixture):
    item = staging_fixture
    port, snapshot = item["port"], item["snapshot"]
    port.environment()
    prepared, reused = port._prepare(snapshot)
    version = f"{sys.version_info.major}{sys.version_info.minor}"
    paths = (prepared.python.parent / f"python{version}._pth").read_text(encoding="utf-8").splitlines()
    assert paths == [
        f"python{version}.zip", "DLLs", ".", str(snapshot / "src"), str(snapshot), "Lib/site-packages",
    ]
    assert not reused
    assert "import site" not in paths
    assert port._command(VerificationCommand("python", ("{python}", "-c", "pass")), prepared) == (
        str(prepared.python), "-B", "-c", "pass",
    )


@pytest.mark.parametrize("source", ["native", "library", "standard", "tool"])
def test_environment_binds_actual_runtime_and_tool_contents_at_unchanged_paths(staging_fixture, source):
    item = staging_fixture
    before = item["port"].environment()
    target = item[source]
    target.write_bytes(target.read_bytes() + b"changed without changing version")
    after = item["port"].environment()
    field = "readonly_tools_sha256" if source == "tool" else "python_runtime_sha256"
    assert after[field] != before[field]
    assert after["python"] == before["python"]


def test_environment_fingerprint_is_independent_of_random_private_snapshot_paths(staging_fixture):
    item = staging_fixture
    port, snapshot = item["port"], item["snapshot"]
    before = port.environment()
    first, _ = port._prepare(snapshot)
    second_snapshot = snapshot.parent / "second-candidate"
    second_snapshot.mkdir()
    second, _ = port._prepare(second_snapshot)
    assert first.python != second.python
    assert first.environment == second.environment == before == port.environment()


@pytest.mark.parametrize("source", ["native", "library", "standard", "tool"])
def test_source_content_drift_between_identity_and_staging_is_rejected(staging_fixture, source):
    item = staging_fixture
    item["port"].environment()
    target = item[source]
    target.write_bytes(target.read_bytes() + b"modified during stage")
    with pytest.raises(LegacyVerificationError) as caught:
        item["port"]._prepare(item["snapshot"])
    assert caught.value.code == "environment_mismatch"


def test_readonly_tool_command_executes_verified_private_copy_not_source_path(staging_fixture):
    item = staging_fixture
    port, snapshot = item["port"], item["snapshot"]
    port.environment()
    prepared, _ = port._prepare(snapshot)
    argv = port._command(VerificationCommand("native", (str(item["tool"]), "--check")), prepared)
    executable = Path(argv[0])
    assert executable != item["tool"]
    assert executable == prepared.python.parent.parent / "tools" / "0" / "check.exe"
    assert executable.read_bytes() == item["tool"].read_bytes()
    assert argv[1:] == ("--check",)


def test_same_snapshot_reuses_only_verified_runtime_and_release_discards_index(staging_fixture):
    item = staging_fixture
    port, snapshot = item["port"], item["snapshot"]
    port.environment()
    first, reused = port._prepare(snapshot)
    assert not reused
    port.environment()
    second, reused = port._prepare(snapshot)
    assert reused and first == second
    assert len(item["stage_calls"]) == 1
    port.release(snapshot)
    third, reused = port._prepare(snapshot)
    assert not reused and third.python != first.python
    assert len(item["stage_calls"]) == 2


@pytest.mark.parametrize("change", ["replace-native", "new-package", "new-pyc", "new-exe", "new-dll"])
def test_same_snapshot_rejects_complete_private_manifest_pollution(staging_fixture, change):
    item = staging_fixture
    port, snapshot = item["port"], item["snapshot"]
    port.environment()
    prepared, _ = port._prepare(snapshot)
    relative = {
        "replace-native": "python.exe", "new-package": "Lib/site-packages/injected/__init__.py",
        "new-pyc": "Lib/site-packages/__pycache__/injected.pyc", "new-exe": "Scripts/injected.exe",
        "new-dll": "DLLs/injected.dll",
    }[change]
    target = prepared.python.parent / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"untrusted injected content")
    port.environment()
    with pytest.raises(LegacyVerificationError) as caught:
        port._prepare(snapshot)
    assert caught.value.code == "environment_mismatch"
    assert len(item["stage_calls"]) == 1


def test_same_snapshot_rejects_changed_source_environment_even_when_copy_unchanged(staging_fixture):
    item = staging_fixture
    port, snapshot = item["port"], item["snapshot"]
    port.environment()
    port._prepare(snapshot)
    item["tool"].write_bytes(b"a new tool at the same trusted source path")
    port.environment()
    with pytest.raises(LegacyVerificationError) as caught:
        port._prepare(snapshot)
    assert caught.value.code == "environment_mismatch"


def test_raw_output_preview_is_bounded_and_does_not_change_full_stream_hash():
    from workspace_orchestrator.integration.verification import _OutputDigest

    payload = b"\x00\xff\r\n" + b"x" * 100000
    captured = _OutputDigest(io.BytesIO(payload))
    captured.drain()
    assert captured.error is None
    assert captured.size == len(payload)
    assert bytes(captured.preview) == payload[:4096]
    assert captured.digest.hexdigest() == hashlib.sha256(payload).hexdigest()


def test_environment_remains_constructible_without_windows_executable_files(tmp_path, monkeypatch):
    """跨平台身份读取不假定存在 python.exe；不伪装该平台可以执行 LPAC。"""
    from types import SimpleNamespace

    from workspace_orchestrator.integration import verification

    native_root = tmp_path / "non-windows-python"
    native_root.mkdir()
    executable = native_root / "python3"
    executable.write_bytes(b"not executed, portable identity fixture")
    standard = native_root / "stdlib"
    standard.mkdir()
    (standard / "os.py").write_bytes(b"# portable standard library fixture\n")
    monkeypatch.setattr(verification, "sys", SimpleNamespace(
        base_prefix=str(native_root), executable=str(executable), platform="linux",
        version_info=SimpleNamespace(major=3, minor=11),
    ))
    monkeypatch.setattr(verification.sysconfig, "get_path", lambda name: str(standard))
    port = WindowsIsolatedCommandPort(python_dependencies=(), python_scripts=())
    assert len(port.environment()["python_runtime_sha256"]) == 64
    with pytest.raises(LegacyVerificationError) as caught:
        port.run(
            VerificationCommand("unsupported", ("{python}", "-c", "pass")),
            snapshot_path=tmp_path, protected_roots=(native_root,), run_id="portable-fixture",
        )
    assert caught.value.code == "isolation_unavailable"


WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="真实 Windows LPAC 验证执行")


class _SealedPythonRuntimeSeed:
    """只由可信 pytest worker 读取；每次克隆前后都核对完整内容清单。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files = self._manifest(root)

    @staticmethod
    def _manifest(root: Path) -> dict[str, str]:
        current = WindowsIsolatedCommandPort._tree_manifest(root)
        if any(source.stat().st_nlink != 1 for source, _ in current.values()):
            raise LegacyVerificationError(
                "unsafe_environment", "LPAC 测试 runtime seed 或克隆不是独立物理文件",
            )
        return {name: digest for name, (_, digest) in current.items()}

    def _assert_unchanged(self) -> None:
        if self._manifest(self.root) != self.files:
            raise LegacyVerificationError(
                "environment_mismatch", "LPAC 测试 runtime seed 内容发生变化",
            )

    def clone_into(self, task_root: Path) -> Path:
        self._assert_unchanged()
        shutil.copytree(self.root / "python", task_root / "python", copy_function=shutil.copy2)
        if self._manifest(task_root) != self.files:
            raise LegacyVerificationError(
                "environment_mismatch", "LPAC 测试 runtime 克隆与可信 seed 不同",
            )
        self._assert_unchanged()
        return task_root / "python" / "python.exe"


class _LpacTestInfrastructure:
    """每个 xdist worker 复用只读 seed/proof，不复用候选、runtime 或 SID。"""

    def __init__(self, seed: _SealedPythonRuntimeSeed) -> None:
        from workspace_orchestrator.integration import verification

        self.seed = seed
        self.launcher = WindowsAppContainerIsolation(
            # Seed 只供可信控制器复制；每次 probe/launch 都把它作为控制面根核对为 LPAC 不可写。
            controller_roots=(Path(verification.__file__).resolve().parents[1], seed.root),
        )

    def new_port(
        self, *, readonly_tools: tuple[Path, ...] = (),
        python_dependencies: tuple[Path, ...] | None = None,
        python_scripts: tuple[Path, ...] | None = None,
    ) -> tuple[WindowsIsolatedCommandPort, list[Path]]:
        stage_calls: list[Path] = []

        def clone(root: Path) -> Path:
            stage_calls.append(root)
            return self.seed.clone_into(root)

        return WindowsIsolatedCommandPort(
            readonly_tools=readonly_tools,
            python_dependencies=python_dependencies,
            python_scripts=python_scripts,
            launcher=self.launcher,
            python_stager=clone,
        ), stage_calls


@pytest.fixture(scope="session")
def lpac_test_infrastructure(tmp_path_factory) -> _LpacTestInfrastructure:
    root = tmp_path_factory.mktemp("lpac-runtime-seed")
    stage_python_runtime(root)
    return _LpacTestInfrastructure(_SealedPythonRuntimeSeed(root))


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-a")
def test_lpac_runtime_seed_clones_are_physical_isolated_and_tamper_evident(
    lpac_test_infrastructure, tmp_path,
):
    seed = lpac_test_infrastructure.seed
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_python = seed.clone_into(first)
    seed.clone_into(second)
    assert all(path.stat().st_nlink == 1 for path in first.rglob("*") if path.is_file())
    assert all(path.stat().st_nlink == 1 for path in second.rglob("*") if path.is_file())

    first_python.write_bytes(b"cross-clone contamination attempt")
    seed._assert_unchanged()
    assert _SealedPythonRuntimeSeed._manifest(second) == seed.files

    local_root = tmp_path / "tampered-seed"
    local_root.mkdir()
    seed.clone_into(local_root)
    local_seed = _SealedPythonRuntimeSeed(local_root)
    (local_root / "python" / "python.exe").write_bytes(b"tampered seed")
    rejected = tmp_path / "rejected-clone"
    rejected.mkdir()
    with pytest.raises(LegacyVerificationError) as caught:
        local_seed.clone_into(rejected)
    assert caught.value.code == "environment_mismatch"


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-a")
def test_default_adapter_rejects_candidate_pytest_impersonation_before_launch(
    repository, tmp_path, lpac_test_infrastructure,
):
    protected = tmp_path / "protected"
    protected.mkdir()
    (repository / "pytest.py").write_bytes(
        b"from pathlib import Path\nPath('forged-tool-ran').write_text('fake PASS')\n",
    )
    (repository / "test_failure.py").write_bytes(b"def test_failure():\n    assert False\n")
    _git(repository, "add", "pytest.py", "test_failure.py")
    _git(repository, "commit", "-m", "tool collision and genuinely failing test")
    port, _ = lpac_test_infrastructure.new_port()
    adapter = LegacyVerificationAdapter(protected_roots=(protected,), command_port=port)
    command = VerificationCommand("pytest", ("{python}", "-m", "pytest", "-q"), 60)
    plan = _plan(repository, adapter.environment, (command,))
    # 这是 Windows 适配器的独立私有克隆/前置拒绝，不声称启动了被拒绝的 LPAC 命令。
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(plan, workspace_path=repository)
    assert caught.value.code == "tool_import_conflict"
    assert caught.value.details["completed_results"] == []
    snapshot = Path(caught.value.details["snapshot_path"])
    assert snapshot.is_dir()
    assert not (snapshot / "forged-tool-ran").exists()


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-b")
def test_real_lpac_trusted_pytest_really_executes_a_failing_candidate_test(
    repository, tmp_path, lpac_test_infrastructure,
):
    protected = tmp_path / "protected"
    protected.mkdir()
    (repository / "test_failure.py").write_text(
        "import os\n"
        "import subprocess\n"
        "import sys\n\n"
        "def test_default_fd_capture_includes_python_and_child_output():\n"
        "    print('python-fd-capture')\n"
        "    subprocess.run([sys.executable, '-S', '-c', "
        "'print(\\\"child-fd-capture\\\", flush=True)'], check=True)\n"
        "    assert False\n\n"
        "def test_capfd_remains_available(capfd):\n"
        "    os.write(1, b'capfd-output')\n"
        "    assert capfd.readouterr().out == 'capfd-output'\n\n"
        "def test_capsys_remains_available(capsys):\n"
        "    print('capsys-output', end='')\n"
        "    assert capsys.readouterr().out == 'capsys-output'\n\n"
        "def test_child_stdin_observes_eof():\n"
        "    subprocess.run([sys.executable, '-S', '-c', "
        "'import sys; assert sys.stdin.buffer.read() == b\\\"\\\"'], check=True)\n",
        encoding="utf-8", newline="\n",
    )
    _git(repository, "add", "test_failure.py")
    _git(repository, "commit", "-m", "genuinely failing test without tool collision")
    port, _ = lpac_test_infrastructure.new_port()
    adapter = LegacyVerificationAdapter(protected_roots=(protected,), command_port=port)
    command = VerificationCommand("pytest", ("{python}", "-m", "pytest", "-q"), 60)
    plan = _plan(repository, adapter.environment, (command,))
    receipt = adapter.execute(plan, workspace_path=repository)
    assert receipt.results[0].returncode == 1, _receipt_diagnostics(receipt)
    evidence = receipt.extra["execution_evidence"][0]
    output = bytes.fromhex(evidence["stdout_preview_hex"])
    assert b"test_failure.py" in output and b"FAILED" in output, _receipt_diagnostics(receipt)
    assert b"python-fd-capture" in output and b"child-fd-capture" in output, (
        _receipt_diagnostics(receipt)
    )
    assert b"1 failed, 3 passed" in output, _receipt_diagnostics(receipt)
    assert evidence["isolation"]["lpac"] is True
    assert evidence["trusted_python_tool"]["module"] == "pytest"
    assert {"pytest", "pytest-xdist", "execnet"} <= evidence["trusted_python_tool"]["distributions"].keys()
    assert evidence["actual_argv"][-2:] == ["-p", "no:debugging"]
    assert evidence["launch_argv"][-2:] == ["-p", "no:debugging"]
    assert evidence["actual_argv"] != evidence["launch_argv"]
    assert evidence["launch_argv"][2] == "-c"
    shim = evidence["pytest_devnull_shim"]
    assert shim["version"] == "2"
    assert hashlib.sha256(evidence["launch_argv"][3].encode()).hexdigest() == shim["sha256"]
    assert shim["devnull"]["relative_task_path"].endswith("/.ai-dev-os-pytest-devnull")
    assert shim["devnull"]["object_type"] == "regular_file"
    assert shim["devnull"]["st_nlink"] == 1
    assert shim["devnull"]["cleaned"] is True
    json.dumps(evidence)
    startup = f"Failed to find real location of {evidence['actual_argv'][0]}\n".encode()
    stderr_prefix = bytes.fromhex(evidence["stderr_preview_hex"])
    assert stderr_prefix.count(startup) <= 1, _receipt_diagnostics(receipt)
    with pytest.raises(PolicyError, match="未全部成功"):
        receipt.validate_for(plan)


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-b")
@pytest.mark.parametrize("layout", ["flat", "src"])
def test_real_lpac_candidate_package_cannot_be_shadowed_by_old_installed_package(
    repository, tmp_path, layout, lpac_test_infrastructure,
):
    dependencies = tmp_path / "old-installed-packages"
    installed = dependencies / "review_demo"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_bytes(b"def answer():\n    return 42\n")
    package = repository / "src" / "review_demo" if layout == "src" else repository / "review_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"def answer():\n    return 0\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "intentionally incorrect candidate package")
    port, _ = lpac_test_infrastructure.new_port(
        python_dependencies=(dependencies,), python_scripts=(),
    )
    adapter = LegacyVerificationAdapter(protected_roots=(dependencies,), command_port=port)
    # 旧安装包 answer()==42；若误用旧包，此断言会返回 0 并造成错误 PASS。
    script = (
        "import review_demo,pathlib;print(review_demo.answer(),flush=True);"
        "print(pathlib.Path(review_demo.__file__).resolve(),flush=True);"
        "assert review_demo.answer()==42"
    )
    command = VerificationCommand("candidate-answer", ("{python}", "-c", script), 30)
    plan = _plan(repository, adapter.environment, (command,))
    receipt = adapter.execute(plan, workspace_path=repository)
    assert receipt.results[0].returncode != 0, _receipt_diagnostics(receipt)
    output = bytes.fromhex(receipt.extra["execution_evidence"][0]["stdout_preview_hex"]).decode("utf-8")
    lines = output.splitlines()
    assert lines[0] == "0", _receipt_diagnostics(receipt)
    imported = Path(lines[1])
    assert imported.parent.name == "review_demo"
    assert imported.parent.parent.name == ("src" if layout == "src" else "candidate")
    assert receipt.extra["execution_evidence"][0]["isolation"]["lpac"] is True
    with pytest.raises(PolicyError, match="未全部成功"):
        receipt.validate_for(plan)


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-a")
def test_real_lpac_private_package_and_bytecode_injection_cannot_reach_next_command(
    repository, tmp_path, lpac_test_infrastructure,
):
    protected = tmp_path / "protected"
    protected.mkdir()
    port, _ = lpac_test_infrastructure.new_port(python_dependencies=(), python_scripts=())
    adapter = LegacyVerificationAdapter(protected_roots=(protected,), command_port=port)
    script = (
        "import pathlib,sys;assert sys.dont_write_bytecode;"
        "root=pathlib.Path(sys.executable).parent/'Lib'/'site-packages';"
        "root.mkdir(parents=True,exist_ok=True);"
        "(root/'injected.py').write_bytes(b'VALUE=42\\n');"
        "(root/'injected.pyc').write_bytes(b'injected bytecode')"
    )
    commands = (
        VerificationCommand("poison-tools", ("{python}", "-c", script), 30),
        VerificationCommand("must-not-run", ("{python}", "-c", "import injected;print(injected.VALUE)"), 30),
    )
    plan = _plan(repository, adapter.environment, commands)
    with pytest.raises(LegacyVerificationError) as caught:
        adapter.execute(plan, workspace_path=repository)
    assert caught.value.code == "environment_mismatch"
    assert caught.value.details["completed_results"] == []
    snapshot = Path(caught.value.details["snapshot_path"])
    assert snapshot.is_dir()
    assert list(snapshot.parent.glob("python-runtime-*/python/Lib/site-packages/injected.pyc"))
    assert not (repository / "injected.py").exists()


@WINDOWS
def test_default_backend_real_candidate_isolated_raw_output_and_cleanup(repository, tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    sentinel = protected / "gate.json"
    sentinel.write_text("trusted", encoding="utf-8")
    script = (
        "import pathlib,sys; assert not pathlib.Path('.git').exists(); "
        "assert pathlib.Path('source.txt').read_bytes()==b'candidate\\x00\\xff\\r\\n'; "
        "pathlib.Path('test-artifact').write_text('private'); "
        f"target=pathlib.Path({str(sentinel)!r})\n"
        "try: target.write_text('forged')\n"
        "except PermissionError: pass\n"
        "else: raise RuntimeError('escaped')\n"
        "sys.stdout.buffer.write(b'raw\\x00\\xff\\r\\n'); "
        "sys.stderr.buffer.write(b'warning\\r\\n')"
    )
    command = VerificationCommand("real-isolated", ("{python}", "-S", "-c", script), 30)
    adapter = LegacyVerificationAdapter(protected_roots=(protected,))
    plan = _plan(repository, adapter.environment, (command,))
    receipt = adapter.execute(plan, workspace_path=repository)
    receipt.validate_for(plan)
    assert sentinel.read_text(encoding="utf-8") == "trusted"
    evidence = receipt.extra["execution_evidence"][0]
    assert receipt.results[0].stdout_sha256 == hashlib.sha256(b"raw\x00\xff\r\n").hexdigest(), (
        _receipt_diagnostics(receipt)
    )
    # CPython getpath 在 LPAC 无法 realpath EXE 时会正常回退并输出一次启动诊断；
    # launcher 已直接绑定 candidate cwd，不再用第二个解释器 wrapper。原始字节不删除。
    startup = f"Failed to find real location of {evidence['actual_argv'][0]}\n".encode()
    stderr = bytes.fromhex(evidence["stderr_preview_hex"])
    assert stderr in (b"warning\r\n", startup + b"warning\r\n"), _receipt_diagnostics(receipt)
    assert evidence["stderr_bytes"] == len(stderr), _receipt_diagnostics(receipt)
    assert receipt.results[0].stderr_sha256 == hashlib.sha256(stderr).hexdigest(), (
        _receipt_diagnostics(receipt)
    )
    assert evidence["isolation"]["lpac"] is True
    assert evidence["cleanup"]["task_sid_removed"] is True
    assert evidence["actual_argv_fingerprint"] == fingerprint(evidence["actual_argv"])
    assert evidence["launch_argv"] == evidence["actual_argv"]
    assert evidence["python_runtime_sha256"] == plan.environment["python_runtime_sha256"]
    assert evidence["readonly_tools_sha256"] == plan.environment["readonly_tools_sha256"]
    assert len(evidence["executed_binary_sha256"]) == 64
    assert not (repository / "test-artifact").exists()


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-b")
def test_default_backend_real_timeout_kills_descendants(
    repository, tmp_path, lpac_test_infrastructure,
):
    protected = tmp_path / "protected"
    protected.mkdir()
    script = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-S','-c',"
        "'import time;time.sleep(60)']);print('started',flush=True);time.sleep(60)"
    )
    command = VerificationCommand("timeout", ("{python}", "-S", "-c", script), 1)
    port, _ = lpac_test_infrastructure.new_port()
    adapter = LegacyVerificationAdapter(protected_roots=(protected,), command_port=port)
    plan = _plan(repository, adapter.environment, (command,))
    receipt = adapter.execute(plan, workspace_path=repository)
    assert receipt.results[0].returncode == 124
    assert receipt.extra["execution_evidence"][0]["timed_out"] is True
    assert receipt.extra["execution_evidence"][0]["cleanup"]["task_sid_removed"] is True
    with pytest.raises(PolicyError, match="未全部成功"):
        receipt.validate_for(plan)


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-a")
def test_existing_python_test_tools_run_inside_private_candidate_directory(
    repository, tmp_path, lpac_test_infrastructure,
):
    protected = tmp_path / "protected"
    protected.mkdir()
    source = repository / "src"
    source.mkdir()
    (source / "demo.py").write_text(
        "def answer() -> int:\n    return 42\n", encoding="utf-8", newline="\n",
    )
    (repository / "test_demo.py").write_text(
        "import pathlib\n\nfrom demo import answer\n\n\n"
        "def test_answer():\n    assert answer() == 42\n"
        "    assert pathlib.Path.cwd().name == 'candidate'\n"
        "    assert not any(pathlib.Path.cwd().glob('python-runtime-*'))\n",
        encoding="utf-8", newline="\n",
    )
    (repository / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
        '[tool.ruff]\ntarget-version = "py311"\n'
        '[tool.mypy]\nstrict = true\n', encoding="utf-8", newline="\n",
    )
    _git(repository, "add", "src", "test_demo.py", "pyproject.toml")
    _git(repository, "commit", "-m", "existing Python project")
    port, stage_calls = lpac_test_infrastructure.new_port()
    adapter = LegacyVerificationAdapter(protected_roots=(protected,), command_port=port)
    commands = (
        VerificationCommand("pytest", (sys.executable, "-m", "pytest", "-q"), 60),
        VerificationCommand("ruff", (sys.executable, "-m", "ruff", "check", "."), 60),
        VerificationCommand("mypy", (sys.executable, "-m", "mypy", "src"), 60),
        VerificationCommand("git", ("git", "diff", "--check"), 60),
    )
    plan = _plan(repository, adapter.environment, commands)
    receipt = adapter.execute(plan, workspace_path=repository)
    assert all(result.returncode == 0 for result in receipt.results), _receipt_diagnostics(receipt)
    receipt.validate_for(plan)
    assert len(receipt.results) == 4
    assert len(stage_calls) == 1
    assert [item["runtime_reused"] for item in receipt.extra["execution_evidence"][:3]] == [False, True, True]
    for execution, tool in zip(receipt.extra["execution_evidence"][:3], ("pytest", "ruff", "mypy"), strict=True):
        assert Path(execution["working_directory"]).name == "candidate"
        assert execution["python_dependencies_sha256"] == plan.environment["python_dependencies_sha256"]
        assert execution["isolation"]["lpac"] is True
        assert execution["cleanup"]["task_sid_removed"] is True
        assert execution["trusted_python_tool"]["module"] == tool
        if tool == "pytest":
            assert execution["actual_argv"][-2:] == ["-p", "no:debugging"]
            assert b"1 passed" in bytes.fromhex(execution["stdout_preview_hex"])
    git_execution = receipt.extra["execution_evidence"][3]
    assert Path(git_execution["working_directory"]).name == "candidate"
    assert not any(str(repository) in argument for argument in git_execution["actual_argv"])
    assert any(
        argument == f"--work-tree={git_execution['working_directory']}"
        for argument in git_execution["actual_argv"]
    )


@WINDOWS
@pytest.mark.xdist_group("lpac-seeded-b")
def test_private_python_ignores_existing_pth_and_cannot_write_dependency_source(
    repository, tmp_path, lpac_test_infrastructure,
):
    dependencies = tmp_path / "existing-dependencies"
    dependencies.mkdir()
    module = dependencies / "installed_dependency.py"
    module.write_text("VALUE = 42\n", encoding="utf-8")
    sentinel = tmp_path / "startup-was-executed"
    (dependencies / "startup.pth").write_text(
        f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('bad')\n", encoding="utf-8",
    )
    port, _ = lpac_test_infrastructure.new_port(
        python_dependencies=(dependencies,), python_scripts=(),
    )
    script = (
        "import installed_dependency,pathlib,sys;assert installed_dependency.VALUE==42;"
        "assert not any('existing-dependencies' in p for p in sys.path);"
        f"target=pathlib.Path({str(module)!r})\n"
        "try: target.write_text('forged')\n"
        "except PermissionError: pass\n"
        "else: raise RuntimeError('dependency source escaped')\n"
        "print('private-installed-dependency')"
    )
    adapter = LegacyVerificationAdapter(protected_roots=(dependencies,), command_port=port)
    command = VerificationCommand("dependencies", ("{python}", "-c", script), 30)
    plan = _plan(repository, adapter.environment, (command,))
    receipt = adapter.execute(plan, workspace_path=repository)
    receipt.validate_for(plan)
    assert not sentinel.exists()
    assert module.read_text(encoding="utf-8") == "VALUE = 42\n"
