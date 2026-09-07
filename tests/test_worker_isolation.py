"""真实临时域负测；不攻击 canonical 或用户文件，不启动 LLM。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from workspace_orchestrator.agent_runtime.stdio import _ProcessTree
from workspace_orchestrator.orchestration.isolation import (
    CodexSandboxIsolation,
    WindowsAppContainerIsolation,
    WorkerIsolationError,
    WorkerIsolationSpec,
    _acl_state,
    _assert_reparse_proofs_unchanged,
    _reparse_target,
    _ReparseProof,
    stage_python_runtime,
    validate_spec,
    worker_environment,
)

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="真实 Windows LPAC 测试")


def test_windows_api_helpers_reject_other_platforms_before_attribute_access(monkeypatch):
    from types import SimpleNamespace

    from workspace_orchestrator.orchestration import isolation

    monkeypatch.setattr(isolation, "sys", SimpleNamespace(platform="linux"))
    for function, arguments in (
        (isolation._require_windows, ("Windows only",)),
        (isolation._windows_library, ("kernel32",)),
        (isolation._open_pipe_handle, (0, 0)),
    ):
        with pytest.raises(isolation.WorkerIsolationError) as caught:
            function(*arguments)
        assert caught.value.code == "platform_unavailable"


def _spec(tmp_path: Path) -> tuple[WorkerIsolationSpec, Path]:
    for name in ("task", "protected", "controller", "tools"):
        (tmp_path / name).mkdir()
    return WorkerIsolationSpec(
        tmp_path / "task", (tmp_path / "protected",), (tmp_path / "tools",), "run", 1
    ), tmp_path / "controller"


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"当前 Windows 不能创建临时 symlink: {exc}")


def _junction_or_skip(link: Path, target: Path) -> None:
    command = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    result = subprocess.run(
        [str(command), "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"当前 Windows 不能创建临时 junction: {result.stderr!r}")


def test_reparse_proof_identity_drift_is_fail_closed() -> None:
    initial = _ReparseProof(
        "C:/root/python3.exe", 1, 0, "python.exe", "C:/root/python.exe",
        2, "03", 2, "04", "file",
    )
    changed = replace(
        initial, raw_target="other.exe", final_path="C:/root/other.exe", target_file_id="05"
    )
    with pytest.raises(WorkerIsolationError) as caught:
        _assert_reparse_proofs_unchanged((initial,), (changed,))
    assert caught.value.code == "linked_path"
    assert f"path={Path('C:/root/python3.exe')}" in str(caught.value)
    assert "target=C:/root/other.exe" in str(caught.value)
    assert "reason=probe_launch_identity_drift" in str(caught.value)


def test_unknown_reparse_tag_is_fail_closed_with_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workspace_orchestrator.orchestration import isolation as isolation_module

    class Kernel:
        @staticmethod
        def DeviceIoControl(
            _handle, _code, _input, _input_size, output, _output_size, returned, _overlapped,
        ) -> bool:
            payload = (0xA0000003).to_bytes(4, "little") + b"\0\0\0\0"
            ctypes.memmove(output, payload, len(payload))
            returned._obj.value = len(payload)
            return True

    api = SimpleNamespace(
        ctypes=ctypes, wt=SimpleNamespace(DWORD=ctypes.c_ulong), kernel=Kernel()
    )
    monkeypatch.setattr(isolation_module, "_win32", lambda: api)
    path = Path("C:/root/alias")
    with pytest.raises(WorkerIsolationError) as caught:
        _reparse_target(object(), path)
    assert caught.value.code == "linked_path"
    assert f"path={path}" in str(caught.value)
    assert "target=<unresolved>" in str(caught.value)
    assert "reason=unsupported_reparse_tag:0xa0000003" in str(caught.value)


@WINDOWS
def test_protected_same_root_file_symlink_is_proved_at_probe_and_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, controller = _spec(tmp_path)
    target = spec.protected_roots[0] / "python.exe"
    target.write_bytes(b"physical target")
    _symlink_or_skip(spec.protected_roots[0] / "python3.exe", target)
    isolation = WindowsAppContainerIsolation(controller_roots=(controller,))
    isolation._proof = {"fixture": True}
    capability = isolation.probe(spec)
    assert capability.supported, capability
    assert capability.evidence["protected_link_count"] >= 1
    attested = next(iter(isolation._attested.values()))
    proof = next(item for item in attested.links if item.path.endswith("python3.exe"))
    assert proof.tag == 0xA000000C
    assert proof.raw_target
    assert Path(proof.final_path) == target
    assert proof.link_volume == proof.target_volume
    assert proof.link_file_id and proof.target_file_id
    assert proof.link_file_id != proof.target_file_id
    assert proof.object_type == "file"
    process = SimpleNamespace(isolation_evidence={})
    monkeypatch.setattr(isolation, "_launch", lambda *args, **kwargs: process)
    assert isolation.launch(spec, [str(spec.task_root / "not-executed.exe")]) is process
    assert process.isolation_evidence["protected_link_count"] >= 1


def test_lpac_working_directory_must_be_a_physical_task_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, controller = _spec(tmp_path)
    candidate = spec.task_root / "candidate"
    candidate.mkdir()
    executable = spec.task_root / "not-executed.exe"
    executable.write_bytes(b"fixture")
    isolation = WindowsAppContainerIsolation(controller_roots=(controller,))
    from workspace_orchestrator.orchestration import isolation as isolation_module

    monkeypatch.setattr(
        isolation_module, "AppContainerProcess", lambda _spec, _command, _env, cwd: cwd,
    )
    assert isolation._launch(
        spec, [str(executable)], working_directory=candidate,
    ) == candidate
    with pytest.raises(WorkerIsolationError) as caught:
        isolation._launch(
            spec, [str(executable)], working_directory=spec.protected_roots[0],
        )
    assert caught.value.code == "unsafe_path"


@WINDOWS
def test_protected_file_symlink_retarget_between_probe_and_launch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, controller = _spec(tmp_path)
    first = spec.protected_roots[0] / "first.exe"
    second = spec.protected_roots[0] / "second.exe"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    link = spec.protected_roots[0] / "python3.exe"
    _symlink_or_skip(link, first)
    isolation = WindowsAppContainerIsolation(controller_roots=(controller,))
    isolation._proof = {"fixture": True}
    assert isolation.probe(spec).supported
    link.unlink()
    _symlink_or_skip(link, second)
    monkeypatch.setattr(isolation, "_launch", lambda *args, **kwargs: pytest.fail("不得启动"))
    with pytest.raises(WorkerIsolationError) as caught:
        isolation.launch(spec, [str(spec.task_root / "not-executed.exe")])
    assert caught.value.code == "linked_path"
    assert "path=" in str(caught.value) and "target=" in str(caught.value)
    assert "reason=probe_launch_identity_drift" in str(caught.value)


@pytest.mark.parametrize("kind", ["broken", "cycle", "outside", "directory"])
@WINDOWS
def test_unsupported_protected_reparse_fails_with_diagnostics(tmp_path: Path, kind: str) -> None:
    spec, controller = _spec(tmp_path)
    link = spec.protected_roots[0] / "alias"
    if kind == "broken":
        _symlink_or_skip(link, spec.protected_roots[0] / "missing.exe")
    elif kind == "cycle":
        other = spec.protected_roots[0] / "other"
        _symlink_or_skip(link, other)
        _symlink_or_skip(other, link)
    elif kind == "outside":
        target = tmp_path / "outside.exe"
        target.write_bytes(b"outside")
        _symlink_or_skip(link, target)
    else:
        target = spec.protected_roots[0] / "directory"
        target.mkdir()
        _junction_or_skip(link, target)
    capability = WindowsAppContainerIsolation(controller_roots=(controller,)).probe(spec)
    assert not capability.supported
    assert capability.evidence["code"] == "linked_path"
    assert "path=" in capability.reason
    assert "target=" in capability.reason
    assert "reason=" in capability.reason


@pytest.mark.parametrize("position", ["root", "ancestor"])
@WINDOWS
def test_protected_root_or_ancestor_reparse_remains_rejected(
    tmp_path: Path, position: str,
) -> None:
    spec, controller = _spec(tmp_path)
    physical = tmp_path / "physical-parent"
    protected = physical / "protected"
    protected.mkdir(parents=True)
    linked = tmp_path / "linked-parent"
    if position == "root":
        _junction_or_skip(linked, protected)
        replacement = linked
    else:
        _junction_or_skip(linked, physical)
        replacement = linked / "protected"
    with pytest.raises(WorkerIsolationError, match="隔离路径包含") as caught:
        validate_spec(replace(spec, protected_roots=(replacement,)), controller_roots=(controller,))
    assert caught.value.code == "linked_path"


@WINDOWS
def test_lpac_staged_python_suspended_and_private(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    executable = stage_python_runtime(spec.task_root)
    sentinel = spec.protected_roots[0] / "gate"
    sentinel.write_text("original")
    before = _acl_state(sentinel)
    script = (
        "import pathlib; pathlib.Path('allowed').write_text('allowed');"
        "\ntry: pathlib.Path(" + repr(str(sentinel)) + ").write_text('bad')"
        "\nexcept PermissionError: print('DENIED')"
        "\nelse: raise RuntimeError('escaped')"
    )
    isolation = WindowsAppContainerIsolation(controller_roots=(controller,))
    process = isolation._launch(spec, [str(executable), "-S", "-X", "utf8", "-c", script])
    tree = None
    try:
        assert not (spec.task_root / "allowed").exists(), "目标不得先于 Job 开始运行"
        assert process.poll() is None
        tree = _ProcessTree(process)
        process.wait(timeout=20)
        tree.kill()
        assert process.stdout is not None and process.stderr is not None
        stdout, stderr = process.stdout.read(), process.stderr.read()
        assert process.returncode == 0, (stdout, stderr, process.returncode)
        assert stdout.strip() == "DENIED"
        assert sentinel.read_text() == "original"
        assert (spec.task_root / "allowed").read_text() == "allowed"
    finally:
        if tree:
            tree.kill()
        process.close()
    assert _acl_state(sentinel) == before
    assert process.cleanup_evidence == {
        "task_sid_removed": True,
        "profile_deleted_before_resume": True,
    }
    assert process.sid not in _acl_state(spec.task_root)


def test_environment_is_an_allowlist_and_paths_are_private(tmp_path: Path) -> None:
    spec, _ = _spec(tmp_path)
    env = worker_environment(
        spec,
        platform={
            "CODEX_THREAD_ID": "root-thread",
            "CODEX_APP_TOKEN": "secret",
            "API_KEY": "secret",
            "PATH": "untrusted",
            "PYTHONPATH": "untrusted",
            "HTTP_PROXY": "secret",
        },
    )
    assert all("secret" not in value and "root-thread" not in value for value in env.values())
    assert "CODEX_THREAD_ID" not in env and "PYTHONPATH" not in env and "HTTP_PROXY" not in env
    for name in ("HOME", "USERPROFILE", "TEMP", "APPDATA", "LOCALAPPDATA", "CODEX_HOME"):
        assert Path(env[name]).is_relative_to(spec.task_root)
    assert "untrusted" not in env["PATH"]


@pytest.mark.parametrize("field", ["protected_roots", "readonly_tools", "controller"])
def test_overlapping_authority_is_rejected(tmp_path: Path, field: str) -> None:
    spec, controller = _spec(tmp_path)
    if field == "controller":
        controller = spec.task_root
    else:
        spec = replace(spec, **{field: (spec.task_root,)})
    with pytest.raises(WorkerIsolationError, match="交叠"):
        validate_spec(spec, controller_roots=(controller,))


@pytest.mark.parametrize("epoch", [0, -1, True, "1"])
def test_invalid_lease_epoch_is_rejected(tmp_path: Path, epoch: object) -> None:
    spec, controller = _spec(tmp_path)
    with pytest.raises(WorkerIsolationError, match="epoch"):
        validate_spec(replace(spec, epoch=epoch), controller_roots=(controller,))


def test_existing_hardlink_cannot_alias_a_protected_file(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    target = spec.protected_roots[0] / "gate"
    target.write_text("original")
    os.link(target, spec.task_root / "alias")
    with pytest.raises(WorkerIsolationError, match="硬链接"):
        validate_spec(spec, controller_roots=(controller,))


def test_codex_setup_is_not_triggered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec, controller = _spec(tmp_path)
    executable = controller / "codex.exe"
    executable.write_bytes(b"test-not-executed")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("不允许调用setup"))
    isolation = CodexSandboxIsolation(executable, controller_roots=(controller,))
    result = isolation.probe(spec)
    assert not result.supported and result.evidence["code"] == "setup_mutation_not_excludable"
    with pytest.raises(WorkerIsolationError):
        isolation.prepare(spec, [str(executable)])


@WINDOWS
def test_public_probe_real_attacks_and_lease_fence(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    executable = stage_python_runtime(spec.task_root)
    command = [str(executable), "-S", "-X", "utf8", "-c", "print('ready')"]
    isolation = WindowsAppContainerIsolation(controller_roots=(controller,))
    with pytest.raises(WorkerIsolationError, match="探测"):
        isolation.launch(spec, command)
    capability = isolation.probe(spec)
    assert capability.supported, capability
    assert capability.evidence["real_process_probe"] is True
    assert capability.evidence["lpac"] is True
    assert set(capability.evidence["denied_operations"]) >= {
        "write",
        "append",
        "truncate",
        "delete",
        "rename",
        "mtime",
        "attributes",
        "acl",
        "control_process",
        "child_token",
        "job_breakaway",
        "network",
        "hardlink",
    }
    assert capability.evidence["cleanup"] == {
        "task_sid_removed": True,
        "profile_deleted_before_resume": True,
    }
    for changed in (
        replace(spec, epoch=2),
        replace(spec, run_id="new"),
        replace(spec, allow_network=True),
    ):
        with pytest.raises(WorkerIsolationError, match="探测"):
            isolation.launch(changed, command)
    # dispatch records an attempt after probing; safe control-plane file creation
    # must not invalidate the unchanged isolation policy.
    ledger = controller / "ledger"
    ledger.mkdir()
    (ledger / "state.json").write_text('{"attempt": 1}', encoding="utf-8")
    new_protected = spec.protected_roots[0] / "new-protected-file"
    new_protected.write_text("trusted")
    process = isolation.launch(spec, command)
    assert (
        process.isolation_evidence["initial_acl_fingerprint"]
        == capability.evidence["acl_fingerprint"]
    )
    assert (
        process.isolation_evidence["launch_acl_fingerprint"]
        != capability.evidence["acl_fingerprint"]
    )
    assert (
        process.isolation_evidence["policy_fingerprint"]
        == capability.evidence["policy_fingerprint"]
    )
    tree = None
    try:
        tree = _ProcessTree(process)
        process.wait(timeout=10)
        tree.kill()
        assert process.stdout is not None
        assert process.stdout.read().strip() == "ready"
    finally:
        if tree:
            tree.kill()
        process.close()
    # Actual authority relaxation remains a hard failure even after a good probe.
    icacls = Path(os.environ["SystemRoot"]) / "System32/icacls.exe"
    subprocess.run(
        [str(icacls), str(new_protected), "/grant", "*S-1-15-2-2:M", "/Q"],
        capture_output=True,
        check=True,
    )
    with pytest.raises(WorkerIsolationError) as rejected:
        isolation.launch(spec, command)
    assert rejected.value.code == "unsafe_acl"


@WINDOWS
def test_lpac_rejects_ambient_package_write_acl(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    # Only this newly created protection fixture is changed, never a user authority root.
    icacls = Path(os.environ["SystemRoot"]) / "System32/icacls.exe"
    subprocess.run(
        [str(icacls), str(spec.protected_roots[0]), "/grant", "*S-1-15-2-2:(OI)(CI)M", "/Q"],
        check=True,
        capture_output=True,
    )
    isolation = WindowsAppContainerIsolation(controller_roots=(controller,))
    capability = isolation.probe(spec)
    assert not capability.supported
    assert capability.evidence["code"] == "unsafe_acl"


@WINDOWS
def test_creation_failure_removes_only_private_sid(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    original = _acl_state(spec.task_root)
    executable = spec.task_root / "not-a-valid.exe"
    executable.write_bytes(b"invalid executable")
    with pytest.raises(OSError):
        WindowsAppContainerIsolation(controller_roots=(controller,))._launch(
            spec, [str(executable)]
        )
    # 原始 ACE 和继承控制标志均须保留，不允许 icacls 物化额外的显式 ACE。
    assert _acl_state(spec.task_root) == original


@WINDOWS
def test_existing_junction_is_rejected(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    command = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    result = subprocess.run(
        [
            str(command),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(spec.task_root / "alias"),
            str(spec.protected_roots[0]),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    with pytest.raises(WorkerIsolationError, match="reparse"):
        validate_spec(spec, controller_roots=(controller,))


@WINDOWS
def test_lpac_job_cancellation_reaps_live_descendant(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    executable = stage_python_runtime(spec.task_root)
    child_script = "import time; print('child-ready', flush=True); time.sleep(60)"
    script = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-S','-X','utf8','-c',{child_script!r}],"
        "stdout=subprocess.PIPE, text=True); "
        "assert child.stdout.readline().strip() == 'child-ready'; "
        "print(child.pid, flush=True); time.sleep(60)"
    )
    isolation = WindowsAppContainerIsolation(controller_roots=(controller,))
    process = isolation._launch(spec, [str(executable), "-S", "-X", "utf8", "-c", script])
    tree = None
    handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        tree = _ProcessTree(process)
        assert process.stdout is not None
        child_pid = int(process.stdout.readline())
        # Retain a handle, not just a PID which the OS could reuse after termination.
        handle = kernel.OpenProcess(0x00100000, False, child_pid)
        assert handle
        assert kernel.WaitForSingleObject(handle, 0) == 258
        assert process.poll() is None
        tree.kill()
        assert kernel.WaitForSingleObject(handle, 5000) == 0
        assert process.wait(timeout=5) != 0
    finally:
        if tree:
            tree.kill()
        if handle:
            kernel.CloseHandle(handle)
        process.close()


@WINDOWS
def test_worker_created_junction_does_not_expand_write_or_cleanup(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    executable = stage_python_runtime(spec.task_root)
    gate = spec.protected_roots[0] / "gate"
    gate.write_text("original")
    original_acl = _acl_state(gate)
    original_directory_acl = _acl_state(gate.parent)
    alias = spec.task_root / "junction"
    cmd = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
    script = (
        "import pathlib,subprocess; "
        f"r=subprocess.run([{cmd!r},'/d','/c','mklink','/J',{str(alias)!r},{str(gate.parent)!r}],"
        "capture_output=True); "
        "error=r.stderr.decode('mbcs').strip()\n"
        "if r.returncode:\n"
        " assert error in ('Access is denied.', '拒绝访问。'), (r.returncode, error)\n"
        " print('CREATION_DENIED')\n"
        "else:\n"
        f" target=pathlib.Path({str(alias / 'gate')!r})\n"
        " try: target.write_text('escaped')\n"
        " except PermissionError: print('WRITE_DENIED')\n"
        " else: raise RuntimeError('escaped')"
    )
    process = WindowsAppContainerIsolation(controller_roots=(controller,))._launch(
        spec, [str(executable), "-S", "-X", "utf8", "-c", script]
    )
    tree = None
    try:
        tree = _ProcessTree(process)
        process.wait(timeout=10)
        tree.kill()
        assert process.stdout is not None and process.stderr is not None
        output, errors = process.stdout.read(), process.stderr.read()
        assert process.returncode == 0, (output, errors)
        assert output.strip() in {"CREATION_DENIED", "WRITE_DENIED"}
    finally:
        if tree:
            tree.kill()
        process.close()
    assert gate.read_text() == "original"
    assert _acl_state(gate) == original_acl
    assert _acl_state(gate.parent) == original_directory_acl


@WINDOWS
def test_protected_ancestor_delete_child_permission_fails_closed(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    icacls = Path(os.environ["SystemRoot"]) / "System32/icacls.exe"
    # Non-inheritable ancestor grant: checking only protected root entries would miss it.
    subprocess.run(
        [str(icacls), str(tmp_path), "/grant", "*S-1-15-2-2:(DC)", "/Q"],
        capture_output=True,
        check=True,
    )
    result = WindowsAppContainerIsolation(controller_roots=(controller,)).probe(spec)
    assert not result.supported and result.evidence["code"] == "unsafe_acl"


@WINDOWS
def test_exit_code_259_is_not_reported_as_success(tmp_path: Path) -> None:
    spec, controller = _spec(tmp_path)
    executable = stage_python_runtime(spec.task_root)
    process = WindowsAppContainerIsolation(controller_roots=(controller,))._launch(
        spec, [str(executable), "-S", "-X", "utf8", "-c", "import sys; sys.exit(259)"]
    )
    tree = None
    try:
        tree = _ProcessTree(process)
        assert process.wait(timeout=10) == 259
        assert process.poll() == 259
    finally:
        if tree:
            tree.kill()
        process.close()
