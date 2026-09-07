"""原生 Git 的可信本地边界与 Task 工作树租约；不向 Worker 授予 Git 写权限。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..adapters.git import GitError
from ..orchestration.contracts import TaskSpec
from ..orchestration.store import OrchestrationStore
from ..workspace import _file_lock


class GitWorkspaceError(GitError):
    """Git 事实或路径身份不满足授权；保留现场并失败关闭。"""


class GitWorkspaceDirtyError(GitWorkspaceError):
    """工作树内容、路径或执行权限与预期 tree 不一致。"""


def _physical(path: Path, *, missing: bool = False, allow_hardlinks: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    if str(path).startswith(("\\\\", "//")) or path == Path(path.anchor):
        raise GitWorkspaceError("Git 控制路径不能是网络、设备或文件系统根")
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise GitWorkspaceError(f"Git 路径不存在：{item}") from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1 and not allow_hardlinks)):
            raise GitWorkspaceError(f"Git 路径含链接、重解析点或硬链接：{item}")
    return path.resolve(strict=not missing)


def _read_regular(path: Path) -> bytes:
    """校验打开前后身份，避免把链接或变化中的文件归集为可信候选。"""
    _physical(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise GitWorkspaceError(f"只能归集普通文件：{path}")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        content = stream.read()
        after = os.fstat(stream.fileno())
    current = path.lstat()

    def identity(item: os.stat_result) -> tuple[int, ...]:
        # Windows 路径 stat 推断扩展名执行位，句柄 stat 不推断；跨接口只比类型。
        mode = stat.S_IFMT(item.st_mode) if os.name == "nt" else item.st_mode
        return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, mode, item.st_nlink,
                getattr(item, "st_file_attributes", 0))

    if before.st_mode != current.st_mode or opened.st_mode != after.st_mode:
        raise GitWorkspaceError(f"归集过程中文件模式发生变化：{path}")
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise GitWorkspaceError(f"归集过程中文件发生变化：{path}")
    if identity(after) != identity(current):
        raise GitWorkspaceError(f"归集过程中路径身份发生变化：{path}")
    return content


def _git_layout(root: Path) -> tuple[Path, Path]:
    root = _physical(root)
    marker = _physical(root / ".git")
    if marker.is_dir():
        git_dir = marker
    else:
        value = _read_regular(marker).decode("utf-8").strip()
        if not value.startswith("gitdir: ") or "\n" in value:
            raise GitWorkspaceError("工作树 .git 指针无效")
        git_dir = _physical(root / value[8:])
    common_marker = git_dir / "commondir"
    common_dir = git_dir
    if common_marker.exists():
        common_dir = _physical(git_dir / _read_regular(common_marker).decode("utf-8").strip())
    if not git_dir.is_dir() or not common_dir.is_dir():
        raise GitWorkspaceError("Git 元数据必须是物理目录")
    for item in (git_dir / "HEAD", common_dir / "config"):
        _physical(item)
    # 原生 Git 仍负责理解对象和 refs；这里只拒绝能把它重定向到域外的布局。
    for name in ("objects", "refs", "worktrees"):
        _physical(common_dir / name, missing=True)
    return git_dir, common_dir


def _relative(name: str) -> str:
    parts = name.split("/")
    reserved = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", re.IGNORECASE)
    if (not name or "\\" in name or any(
        not part or part in (".", "..") or part.casefold() == ".git"
        or part.endswith((".", " ")) or ":" in part or reserved.fullmatch(part)
        or any(ord(char) < 32 for char in part)
        for part in parts
    )):
        raise GitWorkspaceError(f"候选包含不可安全物化的路径：{name!r}")
    return name


def _sha(value: str) -> str:
    if re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is None:
        raise GitWorkspaceError("必须提供完整的 Git SHA")
    return value


class TrustedGit:
    """安全封装原生 Git，所有控制面写者复用 common git dir 的同一把锁。

    此对象只在可信控制进程中使用，不是 OS sandbox。Task 文件可写权限与
    shared .git 不可写权限仍由既有 WorkerIsolationProvider 强制执行。
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = _physical(repo_root)
        self.git_dir, self.common_dir = _git_layout(self.repo_root)
        executable = shutil.which("git")
        if executable is None:
            raise GitWorkspaceError("原生 Git 不可用")
        # Unix 的 Git 安装可把多个受信命令硬链接到同一二进制；Task 文件不放宽。
        self.executable = str(_physical(Path(executable), allow_hardlinks=True))
        self.control_root = self.common_dir / "ai-dev-os-control"
        self._options_cache: dict[Path, tuple[tuple[tuple[str, int, int, bytes], ...], tuple[str, ...]]] = {}

    def _configuration_identity(self, git_dir: Path) -> tuple[tuple[str, int, int, bytes], ...]:
        """仅缓存无 include 的安全选项；每次安全重读内容，不能靠 mtime 代替新鲜事实。"""
        result: list[tuple[str, int, int, bytes]] = []
        paths = dict.fromkeys((self.common_dir / "config", self.common_dir / "config.worktree",
                               git_dir / "config.worktree"))
        for path in paths:
            _physical(path, missing=True)
            if path.exists():
                content = _read_regular(path)
                identity = path.lstat()
                result.append((str(path), identity.st_dev, identity.st_ino, content))
            else:
                result.append((str(path), -1, -1, b""))
        return tuple(result)

    @contextmanager
    def writer(self) -> Iterator[None]:
        _physical(self.control_root, missing=True)
        self.control_root.mkdir(exist_ok=True)
        lock = self.control_root / "repository-writer.lock"
        _physical(lock, missing=True)
        with _file_lock(lock, timeout=60):
            _physical(lock)
            if _git_layout(self.repo_root) != (self.git_dir, self.common_dir):
                raise GitWorkspaceError("仓库 Git 元数据身份已改变")
            yield

    def _command(self, cwd: Path) -> tuple[list[str], dict[str, str]]:
        git_dir, common = _git_layout(cwd)
        if common != self.common_dir:
            raise GitWorkspaceError("工作树不属于本仓库")
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                   GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1")
        options = (
            "core.hooksPath=" + os.devnull, "core.fsmonitor=false", "core.untrackedCache=false",
            "core.attributesFile=" + os.devnull, "core.excludesFile=" + os.devnull,
            "core.sshCommand=", "core.pager=", "core.quotePath=false", "core.sparseCheckout=false",
            "core.sparseCheckoutCone=false", "gc.auto=0", "maintenance.auto=false",
            "commit.gpgSign=false", "tag.gpgSign=false", "protocol.allow=never",
            "credential.helper=", "diff.external=", "diff.trustExitCode=false",
        )
        command = [self.executable, "--no-optional-locks", "--literal-pathspecs",
                   f"--git-dir={git_dir}", f"--work-tree={cwd}"]
        for option in options:
            command.extend(("-c", option))
        identity = self._configuration_identity(git_dir)
        cached = self._options_cache.get(git_dir)
        if cached is not None and cached[0] == identity:
            return [*command, *cached[1]], env
        # git config 只读配置，不触发其中声明的工具。把动态命名的执行钩子也清空，
        # 不仅覆盖一组猜测的 filter/merge driver 名称。
        config = self._invoke([*command, "config", "--null", "--list"], cwd, env, None)
        overrides: list[str] = []
        includes = False
        for entry in config.split(b"\0"):
            key = entry.partition(b"\n")[0].decode("utf-8", errors="strict")
            lowered = key.lower()
            if lowered.startswith(("include.", "includeif.")):
                includes = True
            if (re.fullmatch(r"filter\..+\.(?:clean|smudge|process)", lowered)
                    or re.fullmatch(r"diff\..+\.(?:command|textconv)", lowered)
                    or re.fullmatch(r"merge\..+\.driver", lowered)):
                overrides.extend(("-c", key + "="))
            elif re.fullmatch(r"filter\..+\.required", lowered):
                overrides.extend(("-c", key + "=false"))
        if self._configuration_identity(git_dir) != identity:
            raise GitWorkspaceError("读取配置期间 Git 配置内容或路径身份变化")
        if includes:
            # 不自写 Git include/includeIf 解析器；交由原生命令每次重新读取。
            self._options_cache.pop(git_dir, None)
        else:
            self._options_cache[git_dir] = identity, tuple(overrides)
        return [*command, *overrides], env

    @staticmethod
    def _result(command: list[str], cwd: Path, env: dict[str, str],
                data: bytes | None, *, timeout_seconds: int = 120) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(command, cwd=cwd, env=env, input=data,
                                  capture_output=True, check=False, timeout=timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitWorkspaceError(f"原生 Git 调用失败：{exc}") from exc

    @staticmethod
    def _invoke(command: list[str], cwd: Path, env: dict[str, str],
                data: bytes | None) -> bytes:
        result = TrustedGit._result(command, cwd, env, data)
        if result.returncode != 0:
            # 少数原生 Git 写命令（例如 update-index）把可操作的错误说明写到 stdout。
            # stderr 优先，空时才回退 stdout；限制长度，避免异常把任意大输出带入状态文件。
            detail_bytes = result.stderr.strip() or result.stdout.strip()
            detail = detail_bytes[:4096].decode("utf-8", errors="replace")
            if len(detail_bytes) > 4096:
                detail += "…"
            if not detail:
                detail = f"退出码 {result.returncode}，未返回诊断"
            raise GitWorkspaceError(f"原生 Git 拒绝操作：{detail}")
        return result.stdout

    def run_bytes(self, *args: str, cwd: Path | None = None, input: bytes | None = None,
                  extra_env: dict[str, str] | None = None) -> bytes:
        # 读操作也复用同一可重入锁，避免插件单独调用写命令时遗漏整个仓库边界。
        with self.writer():
            cwd = self.repo_root if cwd is None else _physical(cwd)
            command, env = self._command(cwd)
            if extra_env:
                allowed = {"GIT_INDEX_FILE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                           "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}
                if extra_env.keys() - allowed:
                    raise GitWorkspaceError("受信 Git 调用不接受重定向仓库或执行工具的环境变量")
                env.update(extra_env)
            return self._invoke([*command, *args], cwd, env, input)

    def run(self, *args: str, cwd: Path | None = None, input: bytes | None = None,
            extra_env: dict[str, str] | None = None) -> str:
        return self.run_bytes(*args, cwd=cwd, input=input, extra_env=extra_env).decode(
            "utf-8", errors="strict"
        ).rstrip("\r\n")

    def resolve(self, revision: str, *, cwd: Path | None = None) -> str:
        return _sha(self.run("rev-parse", "--verify", "--end-of-options",
                             revision + "^{commit}", cwd=cwd))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor, descendant = _sha(ancestor), _sha(descendant)
        with self.writer():
            command, env = self._command(self.repo_root)
            result = self._result([*command, "merge-base", "--is-ancestor", ancestor, descendant],
                                  self.repo_root, env, None)
            if result.returncode not in (0, 1):
                raise GitWorkspaceError("原生 Git 无法确认祖先关系：" + result.stderr.decode(
                    "utf-8", errors="replace"
                ).strip())
            return result.returncode == 0

    def checked_diff(self, revision: str, *,
                     timeout_seconds: int = 120) -> subprocess.CompletedProcess[bytes]:
        """受信静态检查只读真实提交；非零输出交给验证回执，不能伪装成成功。"""
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise GitWorkspaceError("静态 Git 检查超时必须是正整数秒")
        with self.writer():
            sha = self.resolve(_sha(revision))
            command, env = self._command(self.repo_root)
            return self._result(
                [*command, "diff-tree", "--check", "--root", "-r", "-m", "--no-commit-id",
                 "--no-ext-diff", "--no-textconv", sha], self.repo_root, env, None,
                timeout_seconds=timeout_seconds,
            )

    def remote_tip(self, remote: str, ref: str) -> str:
        """查询已配置远端的真实 ref，只允许 HTTPS 或可信物理 bare 仓库。

        原生 ls-remote 在全新空目录运行，阻断向上发现仓库，且不加载全局配置；
        URL rewrite、remote helper、SSH/凭据脚本不能随着仓库配置进入传输进程。
        此入口不获取对象、不更新 tracking ref，也不尝试安装或修复认证。
        """
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", remote) is None:
            raise GitWorkspaceError("远端查询只接受已配置的简单 remote 名称")
        if not ref.startswith("refs/"):
            raise GitWorkspaceError("远端查询必须指定完整 ref")
        with self.writer():
            self.run("check-ref-format", ref)
            config: list[tuple[str, str]] = []
            for item in self.run_bytes("config", "--null", "--list").split(b"\0"):
                if item:
                    key, separator, value = item.partition(b"\n")
                    if not separator:
                        value = b""
                    config.append((key.decode("utf-8"), value.decode("utf-8")))
            urls = [value for key, value in config if key == f"remote.{remote}.url"]
            if len(urls) != 1 or not urls[0] or any(ord(char) < 32 for char in urls[0]):
                raise GitWorkspaceError("远端必须配置唯一、非空且无控制字符的 URL")
            url = urls[0]
            if any((key.lower().startswith("url.") and key.lower().endswith(".insteadof")
                    and url.startswith(value)) or
                   (key in (f"remote.{remote}.vcs", f"remote.{remote}.uploadpack") and value)
                   for key, value in config):
                raise GitWorkspaceError("远端配置包含 URL 重写或自定义执行入口，不能自动采用")
            if url.startswith("https://"):
                parsed = urlsplit(url)
                if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                        or parsed.query or parsed.fragment or "\\" in url
                        or any(char.isspace() for char in url)):
                    raise GitWorkspaceError("HTTPS 远端 URL 不允许嵌入凭据或歧义部分")
                protocol = "https"
            else:
                candidate = Path(url)
                if ":" in url and not (os.name == "nt" and candidate.is_absolute()):
                    raise GitWorkspaceError("远端传输只支持 HTTPS 或可信物理本地 bare 目录")
                candidate = _physical(self.repo_root / candidate)
                if not candidate.is_dir() or (candidate / ".git").exists():
                    raise GitWorkspaceError("本地远端必须是物理 bare 仓库")
                for name in ("HEAD", "config", "objects", "refs"):
                    _physical(candidate / name)
                url, protocol = str(candidate), "file"
            env = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("GIT_", "GCM_"))
                   and key.upper() not in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                       GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1",
                       GIT_CEILING_DIRECTORIES=str(self.control_root))
            command = [self.executable, "--no-optional-locks"]
            for option in ("protocol.allow=never", f"protocol.{protocol}.allow=always",
                           "credential.helper=", "credential.interactive=false", "core.askPass=",
                           "core.sshCommand=", "core.hooksPath=" + os.devnull,
                           "http.sslVerify=true", "http.followRedirects=false"):
                command.extend(("-c", option))
            # 临时目录完全由控制进程创建，仅用于阻断 Git 的仓库发现；不存放用户文件。
            with tempfile.TemporaryDirectory(prefix="remote-query-", dir=self.control_root) as folder:
                working = _physical(Path(folder))
                if protocol == "file":
                    bare = self._invoke([*command, f"--git-dir={url}", "rev-parse",
                                         "--is-bare-repository"], working, env, None)
                    if bare.strip() != b"true":
                        raise GitWorkspaceError("本地远端不是 bare 仓库")
                output = self._invoke([*command, "ls-remote", "--exit-code", "--refs",
                                       "--", url, ref], working, env, None)
            found: list[str] = []
            for line in output.decode("utf-8").splitlines():
                columns = line.split("\t")
                if len(columns) == 2 and columns[1] == ref:
                    found.append(_sha(columns[0]))
            if len(found) != 1:
                raise GitWorkspaceError("不能确认远端 ref 的唯一当前值")
            return found[0]

    def tree_entries(self, revision: str) -> dict[str, tuple[str, str]]:
        """只接受普通 tracked blobs；拒绝 symlink、submodule 与跨平台路径别名。"""
        sha = self.resolve(revision)
        result: dict[str, tuple[str, str]] = {}
        normalized: set[str] = set()
        for item in self.run_bytes("ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0"):
            if not item:
                continue
            metadata, path = item.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            name = _relative(path.decode("utf-8", errors="strict"))
            if mode not in ("100644", "100755") or kind != "blob":
                raise GitWorkspaceError("候选不支持符号链接或 submodule，不能假装已隔离")
            if name.casefold() in normalized:
                raise GitWorkspaceError("候选包含跨平台大小写路径冲突")
            normalized.add(name.casefold())
            result[name] = mode, _sha(oid)
        return result

    def export_tree(self, revision: str, destination: Path) -> None:
        """直接导出真实 blob，无 .git、hook、smudge 或其他外部转换。"""
        destination = _physical(destination, missing=True)
        entries = self.tree_entries(revision)
        destination.mkdir(parents=False, exist_ok=False)
        self._materialize(entries, destination)

    def read_blobs(self, object_ids: tuple[str, ...]) -> dict[str, bytes]:
        """复用原生 cat-file 批处理，避免为每个文件启动独立 Git 进程。"""
        identifiers = tuple(dict.fromkeys(_sha(value) for value in object_ids))
        if not identifiers:
            return {}
        output = self.run_bytes("cat-file", "--batch", input=(
            "\n".join(identifiers) + "\n"
        ).encode("ascii"))
        cursor = 0
        result: dict[str, bytes] = {}
        for expected in identifiers:
            end = output.find(b"\n", cursor)
            fields = output[cursor:end].split() if end >= cursor else []
            if len(fields) != 3 or fields[0] != expected.encode("ascii") or fields[1] != b"blob":
                raise GitWorkspaceError("原生 Git 返回了不匹配的 blob 批处理结果")
            try:
                size = int(fields[2])
            except ValueError as exc:
                raise GitWorkspaceError("原生 Git 返回无效对象长度") from exc
            cursor = end + 1
            if size < 0 or output[cursor + size:cursor + size + 1] != b"\n":
                raise GitWorkspaceError("原生 Git 对象内容不完整")
            result[expected] = output[cursor:cursor + size]
            cursor += size + 1
        if cursor != len(output):
            raise GitWorkspaceError("原生 Git 返回了额外对象内容")
        return result

    def _materialize(self, entries: dict[str, tuple[str, str]], destination: Path) -> None:
        existing = self.files(destination)
        if existing.keys() - entries.keys():
            raise GitWorkspaceError("恢复工作树时发现额外文件，保留现场而不覆盖")
        blobs = self.read_blobs(tuple(oid for _, oid in entries.values()))
        for name, (mode, oid) in entries.items():
            path = destination / name
            content = blobs[oid]
            if path.exists():
                if _read_regular(path) != content:
                    raise GitWorkspaceError("恢复工作树会覆盖已有修改，已拒绝")
                continue
            _physical(path.parent, missing=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(content)
            if mode == "100755" and os.name != "nt":
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    @staticmethod
    def files(root: Path, *, preserved_roots: tuple[Path, ...] = ()) -> dict[str, bytes]:
        """不跟随链接、不忽略文件；避免隐藏文件在后续 checkout 中被覆盖。"""
        root = _physical(root)
        result: dict[str, bytes] = {}
        names: set[str] = set()
        for current, directories, files in os.walk(root, followlinks=False):
            if Path(current) == root and ".git" in directories:
                directories.remove(".git")
            directories[:] = [name for name in directories
                              if Path(current) / name not in preserved_roots]
            for name in (*directories, *files):
                path = Path(current) / name
                if path == root / ".git" or path in preserved_roots:
                    continue
                relative = _relative(path.relative_to(root).as_posix())
                _physical(path)
                if path.is_dir():
                    continue
                if relative.casefold() in names:
                    raise GitWorkspaceError("工作树包含大小写别名文件")
                names.add(relative.casefold())
                result[relative] = _read_regular(path)
        return result

    def index_entries(self, root: Path) -> dict[str, tuple[str, str]]:
        for flag in self.run_bytes("ls-files", "-v", "-z", cwd=root).split(b"\0"):
            if flag and flag[:1] != b"H":
                raise GitWorkspaceError("索引包含 skip-worktree、assume-unchanged 或冲突状态")
        result: dict[str, tuple[str, str]] = {}
        for item in self.run_bytes("ls-files", "--stage", "-z", cwd=root).split(b"\0"):
            if not item:
                continue
            metadata, path = item.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            name = _relative(path.decode("utf-8", errors="strict"))
            if stage != "0" or mode not in ("100644", "100755") or name in result:
                raise GitWorkspaceError("索引包含冲突、链接或 submodule")
            result[name] = mode, _sha(oid)
        return result

    def assert_clean(self, path: Path, revision: str | None = None, *,
                     preserved_roots: tuple[Path, ...] = (), native_checkout: bool = False) -> None:
        """默认 exact raw 比较；main 可显式接受 Git 内建 EOL/编码规范化后的同一 blob。

        native_checkout 不启用任何外部 filter；Task 与验证副本仍要求原始字节一致。
        """
        path = _physical(path)
        self._command(path)
        sha = self.resolve("HEAD", cwd=path) if revision is None else self.resolve(revision)
        expected = self.tree_entries(sha)
        preserved = self._checked_preserved(path, expected, preserved_roots)
        if self.index_entries(path) != expected:
            raise GitWorkspaceError("工作树索引与预期提交不一致")
        actual = self.files(path, preserved_roots=preserved)
        if actual.keys() != expected.keys():
            raise GitWorkspaceDirtyError("工作树存在缺失或额外文件（包括被忽略文件）")
        for name, content in actual.items():
            mode, oid = expected[name]
            algorithm = "sha1" if len(oid) == 40 else "sha256"
            digest = hashlib.new(algorithm, f"blob {len(content)}\0".encode("ascii") + content)
            if digest.hexdigest() != oid:
                normalized = self.run("hash-object", "--stdin", "--path=" + name,
                                      input=content, cwd=path) if native_checkout else None
                if normalized != oid:
                    raise GitWorkspaceDirtyError(f"工作树存在未提交修改：{name}")
            if os.name != "nt" and bool((path / name).stat().st_mode & stat.S_IXUSR) != (
                mode == "100755"
            ):
                raise GitWorkspaceDirtyError(f"工作树执行权限与提交不一致：{name}")

    def assert_preserved(self, path: Path, revision: str, *,
                         preserved_roots: tuple[Path, ...] = ()) -> tuple[Path, ...]:
        """CAS 之前先证明新 tree 不会覆盖保留根；不要求旧 checkout 已切换。"""
        path = _physical(path)
        self._command(path)
        return self._checked_preserved(path, self.tree_entries(revision), preserved_roots)

    @staticmethod
    def _checked_preserved(path: Path, expected: dict[str, tuple[str, str]],
                           preserved_roots: tuple[Path, ...]) -> tuple[Path, ...]:
        preserved: list[Path] = []
        for configured in preserved_roots:
            root = _physical(configured, missing=True)
            if root == path:
                raise GitWorkspaceError("不能把整个工作树排除出清洁度检查")
            if path not in root.parents:
                continue
            relative = root.relative_to(path).as_posix()
            if any(name == relative or name.startswith(relative + "/")
                   or relative.startswith(name + "/") for name in expected):
                raise GitWorkspaceError("预期提交与明确保留的控制面路径交叠")
            preserved.append(root)
        return tuple(preserved)


@dataclass(frozen=True, slots=True)
class GitWorkspaceLease:
    requirement_id: str
    task_id: str
    branch: str
    worktree: str
    base_sha: str
    lease_id: str


class LocalGitWorkspaceProvider:
    """用原生 worktree 与已有 OrchestrationStore 恢复 Task 租约。

    release 只释放已干净的租约，保留 branch/worktree 供诊断，不递归删除文件。
    capture_candidate 只允许可信 Runtime 在已确认 Worker 及其子进程退出后调用。
    """

    def __init__(self, repo_root: Path, state_root: Path, worktree_root: Path) -> None:
        self.git = TrustedGit(repo_root)
        self.repo_root = self.git.repo_root
        self.state_root = _physical(state_root, missing=True)
        self.worktree_root = _physical(worktree_root, missing=True)
        for protected in (self.repo_root, self.git.common_dir, self.state_root):
            if (self.worktree_root == protected or self.worktree_root in protected.parents
                    or protected in self.worktree_root.parents):
                raise GitWorkspaceError("Task 工作树根与仓库或可信控制面交叠")
        self.store = OrchestrationStore(self.state_root)

    @staticmethod
    def _key(requirement_id: str, task_id: str) -> str:
        if any(not value.strip() or any(ord(char) < 32 for char in value)
               for value in (requirement_id, task_id)):
            raise GitWorkspaceError("Requirement 与 Task ID 必须是非空且无控制字符的字符串")
        return hashlib.sha256((requirement_id + "\0" + task_id).encode("utf-8")).hexdigest()

    def _data(self) -> dict[str, Any]:
        data: dict[str, Any] = self.store.snapshot()["data"]
        if data.get("repository", str(self.git.common_dir)) != str(self.git.common_dir):
            raise GitWorkspaceError("工作树租约账本不属于本仓库")
        data.setdefault("repository", str(self.git.common_dir))
        data.setdefault("leases", {})
        if not isinstance(data["leases"], dict):
            raise GitWorkspaceError("工作树租约账本格式无效")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        with self.store.transaction():
            lease = self.store.acquire("git-workspaces-" + str(uuid4()), ttl_seconds=300)
            try:
                self.store.mutate(lease, lambda current: current.update(data))
            finally:
                self.store.release(lease)

    @staticmethod
    def _lease(record: dict[str, Any]) -> GitWorkspaceLease:
        fields = ("requirement_id", "task_id", "branch", "worktree", "base_sha", "lease_id")
        if any(not isinstance(record.get(name), str) or not record[name] for name in fields):
            raise GitWorkspaceError("工作树租约缺少稳定身份字段")
        return GitWorkspaceLease(**{name: record[name] for name in fields})

    def _validate(self, record: dict[str, Any]) -> GitWorkspaceLease:
        lease = self._lease(record)
        root = _physical(Path(lease.worktree))
        if root.parent != self.worktree_root:
            raise GitWorkspaceError("租约工作树不在授权根的直接子目录")
        git_dir, common = _git_layout(root)
        if (common != self.git.common_dir or str(git_dir) != record.get("git_dir")
                or git_dir.parent != self.git.common_dir / "worktrees"):
            raise GitWorkspaceError("Task .git 指针被替换或工作树归属漂移")
        backlink = _read_regular(git_dir / "gitdir").decode("utf-8").strip()
        if Path(os.path.abspath(backlink)) != root / ".git":
            raise GitWorkspaceError("Git worktree 反向绑定不匹配")
        if self.git.run("symbolic-ref", "HEAD", cwd=root) != "refs/heads/" + lease.branch:
            raise GitWorkspaceError("Task 分支与租约不一致")
        head = self.git.resolve("HEAD", cwd=root)
        if not self.git.is_ancestor(lease.base_sha, head):
            raise GitWorkspaceError("Task 提交不是授权基线的后代")
        return lease

    def ensure(self, requirement_id: str, task_id: str, *, base_sha: str) -> GitWorkspaceLease:
        base_sha = _sha(base_sha)
        key = self._key(requirement_id, task_id)
        with self.git.writer():
            if self.git.resolve(base_sha) != base_sha:
                raise GitWorkspaceError("工作树基线必须是实际 commit")
            data = self._data()
            record = data["leases"].get(key)
            if record is None:
                self.worktree_root.mkdir(parents=True, exist_ok=True)
                _physical(self.worktree_root)
                lease_id = str(uuid4())
                branch = f"ai-dev-os/task/{key[:20]}-{lease_id[:12]}"
                root = self.worktree_root / key[:32]
                if root.exists():
                    raise GitWorkspaceError("目标工作树路径已由既有文件占用，不能认领或覆盖")
                record = {
                    "requirement_id": requirement_id, "task_id": task_id, "branch": branch,
                    "worktree": str(root), "base_sha": base_sha, "lease_id": lease_id,
                    "status": "preparing", "head_sha": base_sha,
                }
                data["leases"][key] = record
                self._save(data)
            if not isinstance(record, dict) or record.get("base_sha") != base_sha:
                raise GitWorkspaceError("已分配 Task 的基线不能静默改变")
            if record.get("status") == "released":
                raise GitWorkspaceError("该 Task 租约已释放；保留的诊断工作树不能隐式重新认领")
            if record.get("status") == "active":
                return self._validate(record)
            root = Path(record["worktree"])
            _physical(root, missing=True)
            if not root.exists():
                root.mkdir()
                identity = root.stat()
                record["reservation"] = [identity.st_dev, identity.st_ino]
                self._save(data)
            identity = root.stat()
            if record.get("reservation") != [identity.st_dev, identity.st_ino]:
                raise GitWorkspaceError("未完成分配的工作树路径身份无法确认，保留现场")
            marker = root / ".git"
            if not marker.exists():
                if any(root.iterdir()):
                    raise GitWorkspaceError("预留工作树出现未知文件，不能覆盖")
                branch_ref = "refs/heads/" + record["branch"]
                refs = self.git.run("for-each-ref", "--format=%(refname)", branch_ref).splitlines()
                if refs:
                    if refs != [branch_ref] or self.git.resolve(branch_ref) != base_sha:
                        raise GitWorkspaceError("工作树分支被其他写者占用")
                    self.git.run("worktree", "add", "--no-checkout", str(root), record["branch"])
                else:
                    self.git.run("worktree", "add", "--no-checkout", "-b", record["branch"],
                                 str(root), base_sha)
                record["git_dir"] = str(_git_layout(root)[0])
                self._save(data)
            if "git_dir" not in record:
                # 原生 add 已成功但持久回写中断：必须核对双方绑定及唯一预留目录。
                git_dir, common = _git_layout(root)
                if common != self.git.common_dir or git_dir.parent != common / "worktrees":
                    raise GitWorkspaceError("中断的 worktree add 归属不匹配")
                record["git_dir"] = str(git_dir)
            self._validate(record)
            self.git._materialize(self.git.tree_entries(base_sha), root)
            self.git.run("read-tree", base_sha, cwd=root)
            self.git.assert_clean(root, revision=base_sha)
            record["status"] = "active"
            self._save(data)
            return self._lease(record)

    def get(self, requirement_id: str, task_id: str) -> GitWorkspaceLease | None:
        with self.git.writer():
            record = self._data()["leases"].get(self._key(requirement_id, task_id))
            if record is None:
                return None
            if not isinstance(record, dict) or record.get("status") != "active":
                raise GitWorkspaceError("Task 租约尚未分配完成或已经释放")
            return self._validate(record)

    def release(self, requirement_id: str, task_id: str, *, lease_id: str) -> None:
        with self.git.writer():
            data = self._data()
            record = data["leases"].get(self._key(requirement_id, task_id))
            if not isinstance(record, dict) or record.get("lease_id") != lease_id:
                raise GitWorkspaceError("拒绝使用过期或未知租约释放工作树")
            if record.get("status") == "released":
                return
            lease = self._validate(record)
            self.git.assert_clean(Path(lease.worktree))
            if record.get("pending_candidate"):
                raise GitWorkspaceError("候选发布尚未恢复完成，不能释放工作树")
            record["status"] = "released"
            self._save(data)

    def _task(self, task: TaskSpec, data: dict[str, Any]) -> dict[str, Any]:
        task.validate()
        matches = [record for record in data["leases"].values()
                   if isinstance(record, dict) and record.get("task_id") == task.task_id
                   and record.get("worktree") == task.worktree and record.get("branch") == task.branch
                   and record.get("status") == "active"]
        if len(matches) != 1:
            raise GitWorkspaceError("TaskSpec 不匹配唯一有效的 Git 工作树租约")
        self._validate(matches[0])
        return matches[0]

    def capture_candidate(self, task: TaskSpec) -> tuple[str, str]:
        """Worker 已被可信 Runtime 确认终止后，用原始文件生成真实候选提交。

        不接受 Worker 自报 SHA，不运行 git add/commit 的过滤器或 hooks；先记录
        pending intent 再 CAS ref，崩溃重试只能恢复同一候选，不额外生成提交。
        """
        with self.git.writer():
            data = self._data()
            record = self._task(task, data)
            root = Path(record["worktree"])
            parent = record["head_sha"]
            pending = record.get("pending_candidate")
            head = self.git.resolve("HEAD", cwd=root)
            if head not in (parent, pending["sha"] if pending else parent):
                raise GitWorkspaceError("Task 分支被非可信归集写者修改")
            if pending is None:
                if self.git.index_entries(root) != self.git.tree_entries(parent):
                    raise GitWorkspaceError("归集前索引已被修改，不能接纳未知暂存状态")
                files = self.git.files(root)
                ignored = self.git.run_bytes("ls-files", "--others", "--ignored",
                                             "--exclude-standard", "-z", cwd=root)
                if ignored:
                    raise GitWorkspaceError("工作树含被忽略的临时文件，请保留并显式清理后再归集")
                index = self.state_root / ("candidate-index-" + str(uuid4()))
                env = {"GIT_INDEX_FILE": str(index)}
                self.git.run("read-tree", "--empty", cwd=root, extra_env=env)
                original = self.git.tree_entries(parent)
                entries: list[bytes] = []
                for name, content in sorted(files.items()):
                    mode = "100644"
                    if os.name == "nt":
                        mode = original.get(name, (mode, ""))[0]
                    elif (root / name).stat().st_mode & stat.S_IXUSR:
                        mode = "100755"
                    oid = self.git.run("hash-object", "-w", "--stdin", "--no-filters", input=content)
                    entries.append(f"{mode} {oid}\t{name}\0".encode())
                if entries:
                    self.git.run("update-index", "-z", "--index-info", cwd=root,
                                 input=b"".join(entries), extra_env=env)
                tree = _sha(self.git.run("write-tree", cwd=root, extra_env=env))
                old_tree = self.git.run("rev-parse", parent + "^{tree}")
                identity = {"GIT_AUTHOR_NAME": "AI Dev OS", "GIT_AUTHOR_EMAIL": "agent@localhost",
                            "GIT_COMMITTER_NAME": "AI Dev OS",
                            "GIT_COMMITTER_EMAIL": "agent@localhost"}
                sha = parent if tree == old_tree else _sha(self.git.run(
                    "commit-tree", tree, "-p", parent,
                    input=(f"Task candidate {record['requirement_id']}/{task.task_id}\n").encode(),
                    extra_env=identity,
                ))
                pending = {"sha": sha, "tree": tree}
                record["pending_candidate"] = pending
                self._save(data)
                # 临时索引只由控制进程生成；删除精确单文件，不触碰用户工作树。
                _physical(index)
                index.unlink()
            sha, tree = _sha(pending["sha"]), _sha(pending["tree"])
            expected = self.git.tree_entries(sha)
            files = self.git.files(root)
            if files.keys() != expected.keys():
                raise GitWorkspaceError("候选归集后文件集合变化，拒绝发布")
            blobs = self.git.read_blobs(tuple(oid for _, oid in expected.values()))
            for name, content in files.items():
                if blobs[expected[name][1]] != content:
                    raise GitWorkspaceError("候选归集后文件内容变化，拒绝发布")
            if head == parent and sha != parent:
                self.git.run("update-ref", "refs/heads/" + record["branch"], sha, parent)
            self.git.run("read-tree", sha, cwd=root)
            self.git.assert_clean(root, revision=sha)
            record.update(head_sha=sha, candidate_sha=sha, candidate_tree=tree)
            record.pop("pending_candidate", None)
            self._save(data)
            return sha, tree

    def read_candidate(self, task: TaskSpec) -> tuple[str, str]:
        with self.git.writer():
            record = self._task(task, self._data())
            if record.get("pending_candidate") or not record.get("candidate_sha"):
                raise GitWorkspaceError("Task 尚无已持久发布的可信候选")
            root = Path(record["worktree"])
            sha = self.git.resolve("HEAD", cwd=root)
            tree = self.git.run("rev-parse", sha + "^{tree}")
            if sha != record["candidate_sha"] or tree != record["candidate_tree"]:
                raise GitWorkspaceError("候选提交或 tree 已漂移")
            self.git.assert_clean(root, revision=sha)
            return sha, _sha(tree)
