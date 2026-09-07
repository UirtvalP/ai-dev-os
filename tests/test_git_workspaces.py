"""原生临时 Git 仓库覆盖租约、可信候选与失败保留；不操作项目真实 main。"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from workspace_orchestrator.integration.git_integration import GitIntegrationAdapter
from workspace_orchestrator.integration.git_workspace import (
    GitWorkspaceError,
    GitWorkspaceLease,
    LocalGitWorkspaceProvider,
    TrustedGit,
)
from workspace_orchestrator.orchestration.contracts import TaskSpec


def native(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                            capture_output=True, text=True, encoding="utf-8")
    return result.stdout.rstrip("\r\n")


@pytest.fixture
def git_fixture(tmp_path: Path) -> tuple[LocalGitWorkspaceProvider, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    native(repo, "init", "-b", "main")
    native(repo, "config", "user.name", "Fixture")
    native(repo, "config", "user.email", "fixture@example.invalid")
    native(repo, "config", "core.autocrlf", "false")
    (repo / "base.txt").write_bytes(b"base\n")
    native(repo, "add", "base.txt")
    native(repo, "commit", "-m", "base")
    provider = LocalGitWorkspaceProvider(repo, tmp_path / "control", tmp_path / "tasks")
    return provider, native(repo, "rev-parse", "HEAD")


def task_for(lease: GitWorkspaceLease) -> TaskSpec:
    return TaskSpec(lease.task_id, "实现临时夹具", "只修改自身工作树", write_required=True,
                    branch=lease.branch, worktree=lease.worktree)


def test_read_regular_accepts_unchanged_executable(tmp_path):
    from workspace_orchestrator.integration.git_workspace import _read_regular

    source = tmp_path / "check.exe"
    source.write_bytes(b"ordinary executable fixture")
    assert _read_regular(source) == b"ordinary executable fixture"


@pytest.mark.parametrize("changed", [False, True])
def test_read_regular_normalizes_windows_handle_mode_but_rejects_mode_drift(
    tmp_path, monkeypatch, changed,
):
    from workspace_orchestrator.integration import git_workspace

    source = tmp_path / "check.exe"
    source.write_bytes(b"ordinary executable fixture")
    actual_fstat = os.fstat
    calls = 0

    def handle_stat(fd):
        nonlocal calls
        info = actual_fstat(fd)
        calls += 1
        # 模拟 Windows fstat 不包含扩展名推断的执行位，第二次可注入真实模式漂移。
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values["st_mode"] = stat.S_IFREG | (0o600 if changed and calls == 2 else 0o666)
        return SimpleNamespace(**values)

    monkeypatch.setattr(git_workspace, "os", SimpleNamespace(
        **(vars(os) | {"name": "nt", "fstat": handle_stat}),
    ))
    if changed:
        with pytest.raises(GitWorkspaceError, match="模式发生变化"):
            git_workspace._read_regular(source)
    else:
        assert git_workspace._read_regular(source) == b"ordinary executable fixture"


def test_safe_options_reuse_reads_config_content_and_not_only_mtime(git_fixture: Any, monkeypatch) -> None:
    provider, base = git_fixture
    original = TrustedGit._result
    configurations: list[list[str]] = []

    def invoke(command, *args, **kwargs):
        if "config" in command:
            configurations.append(command)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(TrustedGit, "_result", staticmethod(invoke))
    assert provider.git.resolve("HEAD") == base
    assert provider.git.resolve("HEAD") == base
    assert len(configurations) == 1
    path = provider.git.common_dir / "config"
    before = path.stat()
    native(provider.repo_root, "config", "filter.new.clean", "must-never-execute")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert provider.git.resolve("HEAD") == base
    command, _ = provider.git._command(provider.repo_root)
    assert "filter.new.clean=" in command
    assert len(configurations) == 2


@pytest.mark.parametrize("kind", ["include.path", "includeIf.gitdir:**.path"])
def test_included_configuration_is_always_read_by_native_git(git_fixture: Any, tmp_path, monkeypatch, kind):
    provider, base = git_fixture
    included = tmp_path / "included.config"
    included.write_text("[filter \"first\"]\nclean = forbidden\n", encoding="utf-8")
    native(provider.repo_root, "config", kind, included.as_posix())
    original = TrustedGit._result
    reads = []

    def invoke(command, *args, **kwargs):
        if "config" in command:
            reads.append(command)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(TrustedGit, "_result", staticmethod(invoke))
    assert provider.git.resolve("HEAD") == base
    included.write_text("[filter \"second\"]\nclean = forbidden\n", encoding="utf-8")
    command, _ = provider.git._command(provider.repo_root)
    assert "filter.second.clean=" in command
    assert len(reads) == 2


def test_configuration_changed_during_native_read_is_rejected(git_fixture: Any, monkeypatch):
    provider, _ = git_fixture
    original = TrustedGit._result

    def invoke(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if "config" in command:
            native(provider.repo_root, "config", "filter.after-read.clean", "forbidden")
        return result

    monkeypatch.setattr(TrustedGit, "_result", staticmethod(invoke))
    with pytest.raises(GitWorkspaceError, match="配置内容或路径身份变化"):
        provider.git.resolve("HEAD")


def test_workspaces_are_distinct_idempotent_and_restartable(git_fixture: Any) -> None:
    provider, base = git_fixture
    first = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    second = provider.ensure("REQ-X", "TASK-B", base_sha=base)
    assert first.branch != second.branch and first.worktree != second.worktree
    reopened = LocalGitWorkspaceProvider(provider.repo_root, provider.state_root,
                                        provider.worktree_root)
    assert reopened.ensure("REQ-X", "TASK-A", base_sha=base) == first
    assert reopened.get("REQ-X", "TASK-A") == first
    assert reopened.get("REQ-X", "missing") is None
    assert native(provider.repo_root, "rev-parse", "main") == base


def test_concurrent_allocation_uses_single_native_worktree(git_fixture: Any) -> None:
    provider, base = git_fixture
    other = LocalGitWorkspaceProvider(provider.repo_root, provider.state_root,
                                     provider.worktree_root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(item.ensure, "REQ-X", "TASK-A", base_sha=base)
                   for item in (provider, other)]
        leases = [future.result(timeout=60) for future in futures]
    assert leases[0] == leases[1]
    assert native(provider.repo_root, "worktree", "list", "--porcelain").count(
        "branch refs/heads/" + leases[0].branch
    ) == 1


def test_leases_preserve_unknown_persistent_fields(git_fixture: Any) -> None:
    provider, base = git_fixture
    first = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    with provider.git.writer():
        lease = provider.store.acquire("fixture")
        try:
            def extend(data: dict[str, Any]) -> None:
                data["future_extension"] = {"keep": [1, 2]}
                data["leases"][provider._key("REQ-X", "TASK-A")]["future"] = "keep"
            provider.store.mutate(lease, extend)
        finally:
            provider.store.release(lease)
    provider.capture_candidate(task_for(first))
    data = provider.store.snapshot()["data"]
    assert data["future_extension"] == {"keep": [1, 2]}
    assert data["leases"][provider._key("REQ-X", "TASK-A")]["future"] == "keep"


def test_existing_path_and_changed_base_are_not_adopted(git_fixture: Any) -> None:
    provider, base = git_fixture
    path = provider.worktree_root / provider._key("REQ-X", "TASK-A")[:32]
    path.mkdir(parents=True)
    (path / "user.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(GitWorkspaceError, match="占用"):
        provider.ensure("REQ-X", "TASK-A", base_sha=base)
    assert (path / "user.txt").read_text(encoding="utf-8") == "preserve"
    provider.ensure("REQ-X", "TASK-B", base_sha=base)
    (provider.repo_root / "new.txt").write_text("next", encoding="utf-8")
    native(provider.repo_root, "add", ".")
    native(provider.repo_root, "commit", "-m", "next")
    with pytest.raises(GitWorkspaceError, match="基线"):
        provider.ensure("REQ-X", "TASK-B",
                        base_sha=native(provider.repo_root, "rev-parse", "HEAD"))


def test_dirty_lease_resumes_but_cannot_release_or_lose_files(git_fixture: Any) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    path = Path(lease.worktree) / "user.txt"
    path.write_text("preserve", encoding="utf-8")
    assert provider.ensure("REQ-X", "TASK-A", base_sha=base) == lease
    with pytest.raises(GitWorkspaceError, match="租约"):
        provider.release("REQ-X", "TASK-A", lease_id="wrong")
    with pytest.raises(GitWorkspaceError, match="额外"):
        provider.release("REQ-X", "TASK-A", lease_id=lease.lease_id)
    assert path.read_text(encoding="utf-8") == "preserve"
    provider.capture_candidate(task_for(lease))
    provider.release("REQ-X", "TASK-A", lease_id=lease.lease_id)
    provider.release("REQ-X", "TASK-A", lease_id=lease.lease_id)
    assert path.exists()
    assert native(provider.repo_root, "show-ref", "--verify", "refs/heads/" + lease.branch)
    with pytest.raises(GitWorkspaceError, match="释放"):
        provider.ensure("REQ-X", "TASK-A", base_sha=base)


def test_capture_commits_real_raw_files_and_read_is_not_self_report(git_fixture: Any) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    task = task_for(lease)
    with pytest.raises(GitWorkspaceError, match="候选"):
        provider.read_candidate(task)
    root = Path(lease.worktree)
    (root / "base.txt").write_bytes(b"changed\r\n")
    (root / "binary.dat").write_bytes(b"\x00\xff\r\n")
    sha, tree = provider.capture_candidate(task)
    assert sha != base and tree == native(root, "rev-parse", "HEAD^{tree}")
    assert provider.read_candidate(task) == (sha, tree)
    assert provider.capture_candidate(task) == (sha, tree)
    assert native(provider.repo_root, "rev-parse", "main") == base
    assert native(root, "rev-list", "--count", base + "..HEAD") == "1"
    with pytest.raises(GitWorkspaceError, match="匹配"):
        provider.read_candidate(replace(task, branch="main"))
    (root / "binary.dat").write_bytes(b"drift")
    with pytest.raises(GitWorkspaceError, match="未提交"):
        provider.read_candidate(task)


def test_replaced_git_pointer_and_foreign_repo_fail_closed(git_fixture: Any, tmp_path: Path) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    other = tmp_path / "foreign"
    other.mkdir()
    native(other, "init", "-b", "main")
    with pytest.raises(GitWorkspaceError, match="本仓库"):
        provider.git.run("status", "--porcelain", cwd=other)
    # Git for Windows 将 .git 指针设为隐藏文件；r+ 可修改既有 fixture，不变更宿主属性。
    with (Path(lease.worktree) / ".git").open("r+", encoding="utf-8") as marker:
        marker.write("gitdir: " + str(provider.git.git_dir) + "\n")
        marker.truncate()
    with pytest.raises(GitWorkspaceError, match="指针"):
        provider.capture_candidate(task_for(lease))


@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_candidate_rejects_link_escape(git_fixture: Any, tmp_path: Path, link_kind: str) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    outside = tmp_path / "outside.txt"
    outside.write_text("must preserve", encoding="utf-8")
    target = Path(lease.worktree) / "escape.txt"
    try:
        if link_kind == "hardlink":
            os.link(outside, target)
        else:
            target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"本机未提供创建该链接的权限：{exc}")
    with pytest.raises(GitWorkspaceError, match="链接"):
        provider.capture_candidate(task_for(lease))
    assert outside.read_text(encoding="utf-8") == "must preserve"
    assert native(provider.repo_root, "rev-parse", "main") == base


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_raw_clean_rejects_hidden_index_flags(git_fixture: Any, index_flag: str) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    root = Path(lease.worktree)
    native(root, "update-index", index_flag, "base.txt")
    with pytest.raises(GitWorkspaceError, match="索引"):
        provider.git.assert_clean(root)
    with pytest.raises(GitWorkspaceError, match="索引"):
        provider.capture_candidate(task_for(lease))


def test_raw_clean_includes_ignored_files(git_fixture: Any) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    root = Path(lease.worktree)
    (root / ".gitignore").write_text("cache\n", encoding="utf-8")
    provider.capture_candidate(task_for(lease))
    (root / "cache").write_text("preserve", encoding="utf-8")
    with pytest.raises(GitWorkspaceError, match="额外"):
        provider.git.assert_clean(root)
    with pytest.raises(GitWorkspaceError, match="忽略"):
        provider.capture_candidate(task_for(lease))
    assert (root / "cache").exists()


def test_untrusted_config_never_executes_tools(git_fixture: Any, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    provider, base = git_fixture
    marker = tmp_path / "executed"
    command = (f'"{sys.executable}" -c "from pathlib import Path; '
               f"Path({str(marker)!r}).write_text('executed')\"")
    native(provider.repo_root, "config", "core.fsmonitor", command)
    for name in ("clean", "smudge", "process"):
        native(provider.repo_root, "config", "filter.evil." + name, command)
    native(provider.repo_root, "config", "filter.evil.required", "true")
    native(provider.repo_root, "config", "diff.evil.textconv", command)
    native(provider.repo_root, "config", "merge.evil.driver", command)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    root = Path(lease.worktree)
    (root / ".gitattributes").write_text("*.txt filter=evil diff=evil merge=evil\n", encoding="utf-8")
    (root / "base.txt").write_bytes(b"raw\n")
    sha, _ = provider.capture_candidate(task_for(lease))
    provider.git.assert_clean(root)
    destination = tmp_path / "exported"
    provider.git.export_tree(sha, destination)
    assert (destination / "base.txt").read_bytes() == b"raw\n"
    assert not marker.exists() and not (destination / ".git").exists()


def test_raw_export_never_overwrites_existing_destination(git_fixture: Any, tmp_path: Path) -> None:
    provider, base = git_fixture
    destination = tmp_path / "exported"
    provider.git.export_tree(base, destination)
    (destination / "base.txt").write_text("user change", encoding="utf-8")
    with pytest.raises(FileExistsError):
        provider.git.export_tree(base, destination)
    assert (destination / "base.txt").read_text(encoding="utf-8") == "user change"


@pytest.mark.parametrize("point", ["before_ref", "after_ref"])
def test_capture_reconciles_interrupted_ref_publication(
    git_fixture: Any, monkeypatch: pytest.MonkeyPatch, point: str,
) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    root = Path(lease.worktree)
    (root / "base.txt").write_text("candidate\n", encoding="utf-8")
    original = provider.git.run
    failed = False

    def interrupt(*args: str, **kwargs: Any) -> str:
        nonlocal failed
        if args and args[0] == "update-ref" and not failed:
            failed = True
            if point == "after_ref":
                original(*args, **kwargs)
            raise OSError("模拟受控进程中断")
        return str(original(*args, **kwargs))

    monkeypatch.setattr(provider.git, "run", interrupt)
    with pytest.raises(OSError, match="中断"):
        provider.capture_candidate(task_for(lease))
    reopened = LocalGitWorkspaceProvider(provider.repo_root, provider.state_root,
                                        provider.worktree_root)
    sha, tree = reopened.capture_candidate(task_for(lease))
    assert reopened.read_candidate(task_for(lease)) == (sha, tree)
    assert native(root, "rev-list", "--count", base + "..HEAD") == "1"
    assert native(provider.repo_root, "rev-parse", "main") == base


def test_main_ref_update_does_not_hide_old_checkout_during_recovery(git_fixture: Any) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    (Path(lease.worktree) / "base.txt").write_text("candidate\n", encoding="utf-8")
    sha, _ = provider.capture_candidate(task_for(lease))
    with provider.git.writer():
        provider.git.run("update-ref", "refs/heads/main", sha, base)
        provider.git.assert_clean(provider.repo_root, revision=base)
        with pytest.raises(GitWorkspaceError, match="索引"):
            provider.git.assert_clean(provider.repo_root)


def test_linked_state_and_overlapping_worktree_roots_are_rejected(
    git_fixture: Any, tmp_path: Path,
) -> None:
    provider, _ = git_fixture
    with pytest.raises(GitWorkspaceError, match="交叠"):
        LocalGitWorkspaceProvider(provider.repo_root, tmp_path / "control2",
                                  provider.repo_root / "tasks")
    state = provider.state_root / "state.json"
    provider.ensure("REQ-X", "TASK-A", base_sha=native(provider.repo_root, "rev-parse", "HEAD"))
    linked = tmp_path / "linked-state"
    os.link(state, linked)
    with pytest.raises(Exception, match="链接|路径|硬链接|独立普通文件"):
        provider.get("REQ-X", "TASK-A")


def test_writer_lock_is_common_to_linked_worktrees(git_fixture: Any) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    other = TrustedGit(Path(lease.worktree))
    assert provider.git.common_dir == other.common_dir
    with provider.git.writer(), other.writer():
        assert other.resolve("HEAD") == base


def test_main_preserves_only_explicit_nontracked_control_roots(git_fixture: Any) -> None:
    provider, base = git_fixture
    workspace = provider.repo_root / ".workspace"
    workspace.mkdir()
    (workspace / "state.json").write_text("preserve", encoding="utf-8")
    with pytest.raises(GitWorkspaceError, match="额外"):
        provider.git.assert_clean(provider.repo_root)
    provider.git.assert_clean(provider.repo_root, preserved_roots=(workspace,))
    (provider.repo_root / "user.txt").write_text("untracked user file", encoding="utf-8")
    with pytest.raises(GitWorkspaceError, match="额外"):
        provider.git.assert_clean(provider.repo_root, preserved_roots=(workspace,))
    assert (workspace / "state.json").read_text(encoding="utf-8") == "preserve"
    assert native(provider.repo_root, "rev-parse", "main") == base


def test_preservation_cannot_hide_candidate_tracked_control_paths(git_fixture: Any) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    root = Path(lease.worktree)
    (root / ".workspace").mkdir()
    (root / ".workspace" / "attack").write_text("candidate", encoding="utf-8")
    sha, _ = provider.capture_candidate(task_for(lease))
    with pytest.raises(GitWorkspaceError, match="控制面路径交叠"):
        provider.git.assert_clean(provider.repo_root, revision=sha,
                                  preserved_roots=(provider.repo_root / ".workspace",))
    with pytest.raises(GitWorkspaceError, match="整个工作树"):
        provider.git.assert_clean(provider.repo_root, preserved_roots=(provider.repo_root,))


def test_remote_tip_reads_physical_bare_without_mutating_refs(
    git_fixture: Any, tmp_path: Path,
) -> None:
    provider, base = git_fixture
    bare = tmp_path / "remote with spaces.git"
    native(tmp_path, "clone", "--bare", str(provider.repo_root), str(bare))
    native(provider.repo_root, "remote", "add", "origin", str(bare))
    before = (bare / "config").read_bytes()
    assert provider.git.remote_tip("origin", "refs/heads/main") == base
    assert (bare / "config").read_bytes() == before
    assert native(provider.repo_root, "rev-parse", "main") == base
    assert not native(provider.repo_root, "for-each-ref", "refs/remotes/origin")
    with pytest.raises(GitWorkspaceError):
        provider.git.remote_tip("origin", "refs/heads/missing")


@pytest.mark.parametrize("url", [
    "ext::echo unsafe", "git@github.com:example/repo.git", "ssh://github.com/example/repo",
    "custom::path", "http://example.invalid/repo", "https://user:password@example.invalid/repo",
    "https://example.invalid/repo?query=1", "https:///missing-host", "https://example.invalid/a\nb",
])
def test_remote_tip_rejects_unsupported_or_ambiguous_transport(git_fixture: Any, url: str) -> None:
    provider, _ = git_fixture
    native(provider.repo_root, "config", "remote.origin.url", url)
    with pytest.raises(GitWorkspaceError):
        provider.git.remote_tip("origin", "refs/heads/main")


@pytest.mark.parametrize("setting,value", [
    ("url.ext::unsafe.insteadOf", "https://"),
    ("remote.origin.vcs", "unsafe"),
    ("remote.origin.uploadpack", "unsafe-command"),
])
def test_remote_tip_rejects_configured_transport_redirection(
    git_fixture: Any, setting: str, value: str,
) -> None:
    provider, _ = git_fixture
    native(provider.repo_root, "config", "remote.origin.url", "https://example.invalid/repo.git")
    native(provider.repo_root, "config", setting, value)
    with pytest.raises(GitWorkspaceError, match="重写|执行入口"):
        provider.git.remote_tip("origin", "refs/heads/main")


def test_https_query_has_no_repository_or_credential_scripts(
    git_fixture: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, base = git_fixture
    native(provider.repo_root, "config", "remote.origin.url", "https://example.invalid/repo.git")
    for setting in ("core.sshCommand", "core.askPass", "credential.helper",
                    "credential.https://example.invalid.helper"):
        native(provider.repo_root, "config", setting, "unsafe-command")
    monkeypatch.setenv("GIT_ASKPASS", "unsafe-command")
    monkeypatch.setenv("SSH_ASKPASS", "unsafe-command")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "unsafe-command")
    calls: list[list[str]] = []
    original = provider.git._invoke

    def observe(command: list[str], cwd: Path, env: dict[str, str], data: bytes | None) -> bytes:
        if "ls-remote" not in command:
            return bytes(original(command, cwd, env, data))
        calls.append(command)
        assert "unsafe-command" not in " ".join(command)
        assert not any(item.startswith(("--git-dir=", "--work-tree=")) for item in command)
        assert cwd.parent == provider.git.control_root and not (cwd / ".git").exists()
        assert env["GIT_CEILING_DIRECTORIES"] == str(provider.git.control_root)
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "GIT_CONFIG_COUNT" not in env and "GIT_ASKPASS" not in env
        assert "SSH_ASKPASS" not in env
        assert "credential.helper=" in command and "http.sslVerify=true" in command
        assert "protocol.allow=never" in command and "protocol.https.allow=always" in command
        return (base + "\trefs/heads/main\n").encode("ascii")

    monkeypatch.setattr(provider.git, "_invoke", observe)
    assert provider.git.remote_tip("origin", "refs/heads/main") == base
    assert len(calls) == 1


def test_git_error_uses_bounded_stdout_when_stderr_is_empty(
    git_fixture: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = git_fixture
    diagnostic = b"base.txt: needs update\n" + b"x" * 5000

    def reject(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args[0], 1, stdout=diagnostic, stderr=b"")

    monkeypatch.setattr(TrustedGit, "_result", staticmethod(reject))
    with pytest.raises(GitWorkspaceError) as caught:
        provider.git.run("update-index", "--refresh")
    message = str(caught.value)
    assert "base.txt: needs update" in message
    assert message.endswith("…")
    assert len(message.encode("utf-8")) < len(diagnostic)


def test_remote_tip_does_not_adopt_nonbare_or_multiple_urls(git_fixture: Any) -> None:
    provider, _ = git_fixture
    native(provider.repo_root, "config", "remote.origin.url", str(provider.repo_root))
    with pytest.raises(GitWorkspaceError, match="bare"):
        provider.git.remote_tip("origin", "refs/heads/main")
    native(provider.repo_root, "config", "--add", "remote.origin.url", "https://example.invalid/a")
    with pytest.raises(GitWorkspaceError, match="唯一"):
        provider.git.remote_tip("origin", "refs/heads/main")


def test_native_ancestor_handles_unrelated_history_and_missing_object(git_fixture: Any) -> None:
    provider, base = git_fixture
    tree = provider.git.run("rev-parse", base + "^{tree}")
    unrelated = provider.git.run("commit-tree", tree, input=b"unrelated\n", extra_env={
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    })
    assert provider.git.is_ancestor(base, base)
    assert not provider.git.is_ancestor(base, unrelated)
    with pytest.raises(GitWorkspaceError, match="祖先"):
        provider.git.is_ancestor("0" * len(base), base)


def test_checked_diff_returns_actual_output_and_full_safe_command(git_fixture: Any) -> None:
    provider, base = git_fixture
    assert provider.git.checked_diff(base).returncode == 0
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    (Path(lease.worktree) / "base.txt").write_bytes(b"trailing whitespace \n")
    sha, _ = provider.capture_candidate(task_for(lease))
    result = provider.git.checked_diff(sha)
    assert result.returncode != 0
    assert b"base.txt" in result.stdout + result.stderr
    assert "--no-ext-diff" in result.args and "--no-textconv" in result.args
    assert "core.fsmonitor=false" in result.args
    assert "-m" in result.args
    assert result.args[-1] == sha
    with pytest.raises(GitWorkspaceError, match="完整"):
        provider.git.checked_diff("HEAD")


def test_checked_diff_expands_merge_parents_instead_of_silent_empty_success(git_fixture: Any) -> None:
    provider, base = git_fixture
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    (Path(lease.worktree) / "base.txt").write_bytes(b"bad trailing whitespace \n")
    candidate, tree = provider.capture_candidate(task_for(lease))
    merge = provider.git.run("commit-tree", tree, "-p", base, "-p", candidate,
                             input=b"integration merge\n", extra_env={
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    })
    result = provider.git.checked_diff(merge)
    assert result.returncode != 0 and b"base.txt" in result.stdout + result.stderr


@pytest.mark.parametrize("expired", [False, True])
def test_checked_diff_passes_declared_timeout_and_does_not_accept_expiry(
    git_fixture: Any, monkeypatch: pytest.MonkeyPatch, expired: bool,
) -> None:
    provider, base = git_fixture
    original = subprocess.run
    budgets: list[int] = []

    def controlled(command: list[str], **kwargs: Any) -> Any:
        if "diff-tree" in command:
            budgets.append(kwargs["timeout"])
            if expired:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", controlled)
    if expired:
        with pytest.raises(GitWorkspaceError, match="调用失败"):
            provider.git.checked_diff(base, timeout_seconds=3)
    else:
        assert provider.git.checked_diff(base, timeout_seconds=3).returncode == 0
    assert budgets == [3]


def test_batch_blob_read_preserves_binary_and_deduplicates(git_fixture: Any) -> None:
    provider, base = git_fixture
    first = provider.git.run("hash-object", "-w", "--stdin", input=b"one\x00\xff\n")
    second = provider.git.run("hash-object", "-w", "--stdin", input=b"second\r\n")
    assert provider.git.read_blobs((first, second, first)) == {
        first: b"one\x00\xff\n", second: b"second\r\n",
    }
    assert provider.git.read_blobs(()) == {}
    with pytest.raises(GitWorkspaceError, match="blob"):
        provider.git.read_blobs((base,))


@pytest.mark.parametrize("configuration", ["attributes", "autocrlf"])
def test_native_checkout_accepts_git_eol_but_not_dirty_content(
    git_fixture: Any, configuration: str,
) -> None:
    provider, base = git_fixture
    repo = provider.repo_root
    if configuration == "attributes":
        (repo / ".gitattributes").write_text("*.txt text eol=crlf\n", encoding="utf-8")
        native(repo, "add", ".gitattributes")
        native(repo, "commit", "-m", "text policy")
        base = native(repo, "rev-parse", "HEAD")
    else:
        native(repo, "config", "core.autocrlf", "true")
    (repo / "base.txt").write_bytes(b"base\r\n")
    provider.git.assert_clean(repo, native_checkout=True)
    with pytest.raises(GitWorkspaceError, match="未提交"):
        provider.git.assert_clean(repo)
    lease = provider.ensure("REQ-X", "TASK-A", base_sha=base)
    (Path(lease.worktree) / "base.txt").write_bytes(b"candidate\n")
    sha, _ = provider.capture_candidate(task_for(lease))
    with provider.git.writer():
        provider.git.run("update-ref", "refs/heads/main", sha, base)
        provider.git.assert_clean(repo, revision=base, native_checkout=True)
        GitIntegrationAdapter(provider.git).reconcile_checkout(base, sha, str(repo))
        provider.git.assert_clean(repo, revision=sha, native_checkout=True)
    assert (repo / "base.txt").read_bytes() == b"candidate\r\n"
    (repo / "base.txt").write_bytes(b"user edit\r\n")
    with pytest.raises(GitWorkspaceError, match="未提交"):
        provider.git.assert_clean(repo, revision=sha, native_checkout=True)
    with pytest.raises(GitWorkspaceError, match="未提交"):
        GitIntegrationAdapter(provider.git).reconcile_checkout(base, sha, str(repo))
    assert (repo / "base.txt").read_bytes() == b"user edit\r\n"


def test_native_checkout_does_not_enable_external_clean_filter(
    git_fixture: Any, tmp_path: Path,
) -> None:
    provider, _ = git_fixture
    repo = provider.repo_root
    (repo / ".gitattributes").write_text("*.txt text eol=crlf filter=evil\n", encoding="utf-8")
    native(repo, "add", ".gitattributes")
    native(repo, "commit", "-m", "attribute policy")
    marker = tmp_path / "filter-executed"
    command = (f'"{sys.executable}" -c "from pathlib import Path; '
               f"Path({str(marker)!r}).write_text('executed')\"")
    native(repo, "config", "filter.evil.clean", command)
    native(repo, "config", "filter.evil.required", "true")
    (repo / "base.txt").write_bytes(b"base\r\n")
    provider.git.assert_clean(repo, native_checkout=True)
    (repo / "base.txt").write_bytes(b"different\r\n")
    with pytest.raises(GitWorkspaceError, match="未提交"):
        provider.git.assert_clean(repo, native_checkout=True)
    assert not marker.exists()
