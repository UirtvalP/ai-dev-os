"""Phase 3 最小验证适配层：真实候选副本、受控进程和结构化结果。

V1 的命令执行语义继续使用 argv/超时/退出码；不能直接调用其在当前用户权限下
执行项目代码的 runner。这里只复用已验收的 LPAC launcher 和进程树回收边界，
不实现 Phase 4 的策略选择、远程执行或验证恢复框架。
"""

from __future__ import annotations

import copy
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import machinery, metadata
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast
from uuid import uuid4

from ..agent_runtime.stdio import _ProcessTree
from ..orchestration.contracts import (
    PolicyError,
    VerificationCommand,
    VerificationCommandResult,
    VerificationPlan,
    VerificationReceiptEnvelope,
    fingerprint,
)
from ..orchestration.isolation import (
    WindowsAppContainerIsolation,
    WorkerIsolationError,
    WorkerIsolationSpec,
    _final_handle_path,
    _handle_identity,
    _open_proof_handle,
    _physical_path,
    _win32,
    stage_python_runtime,
)
from .git_workspace import TrustedGit


class LegacyVerificationError(PolicyError):
    """证据未知或未能安全结束；保留诊断副本，不生成可通过的回执。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message)
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """可信命令端口报告的实际执行事实，不是 Worker 可提交的 JSON。"""

    returncode: int
    stdout_sha256: str
    stderr_sha256: str
    duration_seconds: float
    cleanup_confirmed: bool
    evidence: dict[str, Any] = field(default_factory=dict)


class VerificationCommandPort(Protocol):
    """仅由控制器安装的执行实现；注入 fixture 不代表操作系统隔离已经证明。"""

    def environment(self) -> dict[str, str]: ...

    def run(
        self, command: VerificationCommand, *, snapshot_path: Path,
        protected_roots: tuple[Path, ...], run_id: str,
    ) -> CommandExecution: ...


_OUTPUT_PREVIEW_LIMIT = 4096
_PYTEST_DEVNULL_SHIM_VERSION = "2"
_PYTEST_DEVNULL_NAME = ".ai-dev-os-pytest-devnull"
_PYTEST_DEVNULL_SHIM = r"""
import os
import runpy
import stat
import sys

module = sys.argv[1]
relative_devnull = sys.argv[2]
interpreter_count = int(sys.argv[3])
arguments = sys.argv[4:]
original = list(sys.orig_argv)
command_index = 1 + interpreter_count
if (
    interpreter_count < 0
    or original[command_index:command_index + 1] != ["-c"]
    or original[command_index + 2:] != [
        module, relative_devnull, str(interpreter_count), *arguments,
    ]
):
    raise RuntimeError("pytest private devnull argv proof failed")
interpreter = original[1:command_index]
sys.orig_argv = [original[0], *interpreter, "-m", module, *arguments]
sys.argv = [module, *arguments]
task_root = os.path.dirname(os.path.abspath(os.getcwd()))
devnull = os.path.abspath(os.path.join(task_root, relative_devnull))
if os.path.commonpath((task_root, devnull)) != task_root:
    raise RuntimeError("pytest private devnull escaped task root")
fd = os.open(devnull, os.O_RDWR)
try:
    opened = os.fstat(fd)
    lexical = os.lstat(devnull)
    identity = lambda item: (item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode))
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(lexical.st_mode)
        or identity(opened) != identity(lexical)
        or opened.st_nlink != 1
        or lexical.st_nlink != 1
        or opened.st_size != 0
        or lexical.st_size != 0
        or getattr(lexical, "st_file_attributes", 0) & 0x400
    ):
        raise RuntimeError("pytest private devnull identity check failed")
finally:
    os.close(fd)
os.devnull = devnull
runpy.run_module(module, run_name="__main__", alter_sys=True)
""".strip()
_PYTEST_DEVNULL_SHIM_SHA256 = hashlib.sha256(_PYTEST_DEVNULL_SHIM.encode()).hexdigest()


@dataclass
class _HeldPytestDevnull:
    path: Path
    task_root: Path
    handle: Any
    volume_serial: int
    file_id: str
    attributes: int

    def close(self) -> None:
        if self.handle is not None:
            _win32().kernel.CloseHandle(self.handle)
            self.handle = None


@dataclass
class _OutputDigest:
    stream: BinaryIO
    digest: Any = field(default_factory=hashlib.sha256)
    size: int = 0
    error: Exception | None = None
    preview: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)

    @property
    def suffix_preview(self) -> bytes:
        """只返回 prefix 尚未覆盖的有界尾部；短输出不重复同一批字节。"""

        remaining = self.size - len(self.preview)
        return bytes(self.tail[-min(remaining, _OUTPUT_PREVIEW_LIMIT):]) if remaining > 0 else b""

    @property
    def preview_complete(self) -> bool:
        return self.size <= len(self.preview) + len(self.suffix_preview)

    def drain(self) -> None:
        try:
            while data := self.stream.read(65536):
                self.digest.update(data)
                self.size += len(data)
                if len(self.preview) < _OUTPUT_PREVIEW_LIMIT:
                    self.preview.extend(data[:_OUTPUT_PREVIEW_LIMIT - len(self.preview)])
                self.tail.extend(data)
                if len(self.tail) > _OUTPUT_PREVIEW_LIMIT:
                    del self.tail[:-_OUTPUT_PREVIEW_LIMIT]
        except Exception as exc:  # noqa: BLE001 -- 后台错误必须带回控制线程，禁止签发部分输出摘要。
            self.error = exc


def _completed_output_evidence(stdout: bytes, stderr: bytes) -> dict[str, Any]:
    """为已在内存中的可信命令输出生成与流式 capture 相同的无歧义预览。"""

    def preview(data: bytes) -> tuple[bytes, bytes, bool]:
        prefix = data[:_OUTPUT_PREVIEW_LIMIT]
        remaining = len(data) - len(prefix)
        suffix = data[-min(remaining, _OUTPUT_PREVIEW_LIMIT):] if remaining > 0 else b""
        return prefix, suffix, len(data) <= len(prefix) + len(suffix)

    stdout_prefix, stdout_suffix, stdout_complete = preview(stdout)
    stderr_prefix, stderr_suffix, stderr_complete = preview(stderr)
    return {
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_preview_hex": stdout_prefix.hex(),
        "stderr_preview_hex": stderr_prefix.hex(),
        "stdout_suffix_preview_hex": stdout_suffix.hex(),
        "stderr_suffix_preview_hex": stderr_suffix.hex(),
        "stdout_preview_complete": stdout_complete,
        "stderr_preview_complete": stderr_complete,
        "output_preview_limit_bytes": _OUTPUT_PREVIEW_LIMIT,
    }


_SourceManifest = dict[str, tuple[Path, str]]


def _is_windows_lpac_platform() -> bool:
    """保留运行时平台门禁，避免类型检查器按目标平台裁掉后续 Windows 实现。"""
    return sys.platform == "win32"


@dataclass(frozen=True)
class _EnvironmentSources:
    runtime: _SourceManifest
    dependencies: _SourceManifest
    tools: _SourceManifest
    environment: dict[str, str]


@dataclass(frozen=True)
class _PrivateRuntime:
    python: Path
    files: dict[str, str]
    environment: dict[str, str]
    snapshot_path: Path


def _source_digest(path: Path) -> str:
    """读取实际普通文件内容并核对打开前后身份；不以 mtime 缓存代替摘要。"""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
            or getattr(before, "st_file_attributes", 0) & 0x400):
        raise LegacyVerificationError("unsafe_environment", "工具来源不是物理普通文件")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    path_after = path.lstat()
    identities = {
        (item.st_dev, item.st_ino,
         stat.S_IFMT(item.st_mode) if os.name == "nt" else item.st_mode,
         item.st_size, item.st_mtime_ns, item.st_nlink, getattr(item, "st_file_attributes", 0))
        for item in (before, opened, after, path_after)
    }
    # Windows 路径 stat 会按 EXE 后缀推导 executable 位，句柄 fstat 不会。
    # 跨接口比较文件类型；同一接口的前后模式仍完整比较，不能吞掉实际权限变化。
    if (len(identities) != 1 or before.st_mode != path_after.st_mode
            or opened.st_mode != after.st_mode
            or not all(stat.S_ISREG(item.st_mode) for item in (opened, after, path_after))
            or getattr(path_after, "st_file_attributes", 0) & 0x400):
        raise LegacyVerificationError("environment_mismatch", "读取期间工具来源发生变化")
    return digest


def _manifest_fingerprint(manifest: _SourceManifest) -> str:
    return fingerprint({name: digest for name, (_, digest) in manifest.items()})


class WindowsIsolatedCommandPort:
    """复用现有 Python 环境、原生工具和 LPAC，不安装依赖或执行全局 .pth。

    只支持可信当前 Python（包括 V1 的 {python}）、以及显式只读工具绝对路径。
    site-packages 来自控制器当前 sysconfig，复制后通过 CPython 官方 ._pth 接线；
    当前环境中的 Ruff 原生 EXE 也复制到私有 Scripts，原包负责查找和启动它。
    """

    def __init__(
        self, *, readonly_tools: tuple[Path, ...] = (),
        python_dependencies: tuple[Path, ...] | None = None,
        python_scripts: tuple[Path, ...] | None = None,
        launcher: WindowsAppContainerIsolation | None = None,
        python_stager: Callable[[Path], Path] | None = None,
    ) -> None:
        self.readonly_tools = tuple(_physical_path(path.resolve(strict=True)) for path in readonly_tools)
        dependencies = python_dependencies if python_dependencies is not None else tuple(
            dict.fromkeys(Path(sysconfig.get_path(name)) for name in ("purelib", "platlib"))
        )
        self.python_dependencies = tuple(dict.fromkeys(
            _physical_path(path.resolve(strict=True)) for path in dependencies
        ))
        if python_scripts is None:
            ruff = Path(sysconfig.get_path("scripts")) / ("ruff.exe" if os.name == "nt" else "ruff")
            python_scripts = (ruff,) if ruff.is_file() else ()
        self.python_scripts = tuple(
            _physical_path(path.resolve(strict=True), directory=False, allow_hardlinks=True)
            for path in python_scripts
        )
        self.launcher = launcher if launcher is not None else WindowsAppContainerIsolation(
            controller_roots=(Path(__file__).resolve().parents[1],),
        )
        # 注入点只用于以独立物理副本复用测试基础设施；生产默认仍执行真实 cold stage。
        self._python_stager = python_stager
        # 只在当前控制线程的一次 snapshot 回合复用，不共享可被其他运行污染的缓存。
        self._local = threading.local()

    def _dependencies(self) -> dict[str, tuple[Path, str]]:
        """只枚举既有文件，不导入包、解释 RECORD 或执行 editable/.pth 启动代码。"""
        result: dict[str, tuple[Path, str]] = {}
        for root in self.python_dependencies:
            for current, directories, files in os.walk(root, followlinks=False):
                directories[:] = sorted(name for name in directories if name != "__pycache__")
                for name in directories:
                    _physical_path(Path(current) / name)
                for name in sorted(files):
                    if name.endswith((".pth", ".pyc", ".pyo")):
                        continue
                    source = _physical_path(
                        Path(current) / name, directory=False, allow_hardlinks=True,
                    )
                    relative = "Lib/site-packages/" + source.relative_to(root).as_posix()
                    if relative in result:
                        raise LegacyVerificationError("dependency_conflict", "既有 Python 依赖路径冲突")
                    result[relative] = source, _source_digest(source)
        for source in self.python_scripts:
            relative = "Scripts/" + source.name
            if relative in result:
                raise LegacyVerificationError("dependency_conflict", "既有 Python Scripts 名称冲突")
            result[relative] = source, _source_digest(source)
        return result

    def _runtime_sources(self) -> _SourceManifest:
        """绑定既有 staging 实际读取的原生文件/stdlib，以及当前解释器入口。"""
        root = _physical_path(Path(sys.base_prefix).resolve(strict=True))
        executable = _physical_path(
            Path(sys.executable).resolve(strict=True), directory=False, allow_hardlinks=True,
        )
        result = {"controller-executable": (executable, _source_digest(executable))}
        version = f"{sys.version_info.major}{sys.version_info.minor}"
        for name in ("python.exe", f"python{version}.dll", "python3.dll",
                     "vcruntime140.dll", "vcruntime140_1.dll"):
            source = root / name
            if source.is_file():
                result[name] = source, _source_digest(source)
        libraries = root / "DLLs"
        if libraries.exists():
            _physical_path(libraries)
            for source in sorted(libraries.iterdir()):
                if source.is_file() and source.suffix.lower() in {".dll", ".pyd"}:
                    result["DLLs/" + source.name] = source, _source_digest(source)
        standard = _physical_path(Path(sysconfig.get_path("stdlib")).resolve(strict=True))
        excluded = {"__pycache__", "site-packages", "test", "tests", "idlelib",
                    "tkinter", "turtledemo", "ensurepip"}
        for current, directories, files in os.walk(standard, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in excluded)
            for name in directories:
                _physical_path(Path(current) / name)
            for name in sorted(files):
                if name.endswith(".py"):
                    source = Path(current) / name
                    result["stdlib/" + source.relative_to(standard).as_posix()] = (
                        source, _source_digest(source),
                    )
        return result

    @staticmethod
    def _tree_manifest(root: Path) -> _SourceManifest:
        """完整物理文件清单，包括新增文件；不能忽略注入的 pycache/启动文件。"""
        _physical_path(root)
        result: _SourceManifest = {}
        for current, directories, files in os.walk(root, followlinks=False):
            for name in sorted(directories):
                _physical_path(Path(current) / name)
            for name in sorted(files):
                source = _physical_path(Path(current) / name, directory=False, allow_hardlinks=True)
                result[source.relative_to(root).as_posix()] = source, _source_digest(source)
        return result

    def _tool_sources(self) -> _SourceManifest:
        return {
            f"{index}/{relative}": identity
            for index, root in enumerate(self.readonly_tools)
            for relative, identity in self._tree_manifest(root).items()
        }

    def _environment_sources(self) -> _EnvironmentSources:
        runtime, dependencies, tools = self._runtime_sources(), self._dependencies(), self._tool_sources()
        environment = {
            "platform": sys.platform,
            "machine": platform.machine() or "unknown",
            "python": platform.python_version(),
            "isolation_backend": "windows-appcontainer",
            "python_runtime_sha256": _manifest_fingerprint(runtime),
            "python_dependencies_sha256": _manifest_fingerprint(dependencies),
            "readonly_tools_sha256": _manifest_fingerprint(tools),
            "python_path_policy": "private-copy-candidate-first-no-site-pth-v2",
            "python_tool_policy": "trusted-distribution-entry-import-names-lpac-no-debugging-v2",
        }
        return _EnvironmentSources(runtime, dependencies, tools, environment)

    def environment(self) -> dict[str, str]:
        sources = self._environment_sources()
        # run 消费紧邻环境校验刚读取的内容清单，避免复制前立刻重复同一来源扫描。
        self._local.sources = sources
        return dict(sources.environment)

    @staticmethod
    def _copy_sources(sources: _SourceManifest, destination: Path) -> None:
        for relative, (source, expected) in sources.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if _source_digest(target) != expected:
                raise LegacyVerificationError("environment_mismatch", "复制期间可信工具内容发生变化")

    @staticmethod
    def _assert_staged_python(binary: Path, sources: _SourceManifest) -> None:
        version = f"{sys.version_info.major}{sys.version_info.minor}"
        expected_standard = {
            name.removeprefix("stdlib/"): digest
            for name, (_, digest) in sources.items() if name.startswith("stdlib/")
        }
        with zipfile.ZipFile(binary.parent / f"python{version}.zip") as bundle:
            entries = bundle.namelist()
            if len(entries) != len(set(entries)) or {
                name: hashlib.sha256(bundle.read(name)).hexdigest() for name in entries
            } != expected_standard:
                raise LegacyVerificationError("environment_mismatch", "私有 Python 标准库与环境绑定不同")
        expected_native = {
            name: digest for name, (_, digest) in sources.items()
            if name != "controller-executable" and not name.startswith("stdlib/")
        }
        actual_native = {
            name: digest for name, (_, digest) in WindowsIsolatedCommandPort._tree_manifest(binary.parent).items()
            if name != f"python{version}.zip"
        }
        if actual_native != expected_native or "python.exe" not in actual_native:
            raise LegacyVerificationError("environment_mismatch", "私有 Python EXE/DLL 与环境绑定不同")

    def _stage_python(self, snapshot_path: Path, sources: _EnvironmentSources) -> _PrivateRuntime:
        # 工具是 candidate 的兄弟目录，pytest/ruff 的项目扫描不会递归进入依赖。
        runtime_root = snapshot_path.parent / f"python-runtime-{uuid4().hex}"
        runtime_root.mkdir()
        binary = (self._python_stager or stage_python_runtime)(runtime_root)
        self._assert_staged_python(binary, sources.runtime)
        self._copy_sources(sources.dependencies, binary.parent)
        self._copy_sources(sources.tools, runtime_root / "tools")
        version = f"{sys.version_info.major}{sys.version_info.minor}"
        # 可信 stdlib/DLL 在前；候选源码必须覆盖旧安装包，src 优先于根目录。
        search_paths = [f"python{version}.zip", "DLLs", "."]
        if (snapshot_path / "src").is_dir():
            search_paths.append(str(snapshot_path / "src"))
        search_paths.extend((str(snapshot_path), "Lib/site-packages"))
        # 不包含 import site：CPython 将忽略注册表、PYTHONPATH、用户 site 和 .pth。
        (binary.parent / f"python{version}._pth").write_text(
            "\n".join(search_paths) + "\n", encoding="utf-8", newline="\n",
        )
        return _PrivateRuntime(
            binary, {name: digest for name, (_, digest) in self._tree_manifest(runtime_root).items()},
            dict(sources.environment), snapshot_path,
        )

    def _assert_private_runtime(self, prepared: _PrivateRuntime, snapshot_path: Path) -> None:
        root = _physical_path(prepared.python.parent.parent)
        if root.parent != snapshot_path.parent or not root.name.startswith("python-runtime-"):
            raise LegacyVerificationError("environment_mismatch", "私有工具目录身份发生变化")
        current = self._tree_manifest(root)
        digests = {name: digest for name, (_, digest) in current.items()}
        changed = sorted(
            name for name in prepared.files.keys() | digests.keys()
            if prepared.files.get(name) != digests.get(name)
        )
        if any(source.stat().st_nlink != 1 for source, _ in current.values()) or changed:
            raise LegacyVerificationError(
                "environment_mismatch", "验证命令改写了私有工具内容",
                details={"changed_tool_files": changed[:25]},
            )

    def _prepare(self, snapshot_path: Path) -> tuple[_PrivateRuntime, bool]:
        sources: _EnvironmentSources | None = getattr(self._local, "sources", None)
        self._local.sources = None  # 清单只消费一次，不能当作跨命令来源内容缓存。
        if sources is None:
            sources = self._environment_sources()
        cached: dict[Path, _PrivateRuntime] = getattr(self._local, "private", {})
        prepared = cached.get(snapshot_path)
        reused = prepared is not None
        if prepared is None:
            prepared = self._stage_python(snapshot_path, sources)
            cached[snapshot_path] = prepared
            self._local.private = cached
        elif prepared.environment != sources.environment:
            raise LegacyVerificationError("environment_mismatch", "可信工具来源在验证回合中变化")
        self._assert_private_runtime(prepared, snapshot_path)
        return prepared, reused

    def release(self, snapshot_path: Path) -> None:
        # 仅移除内存索引；目录生命周期/失败诊断保全仍由原 adapter 负责。
        getattr(self._local, "private", {}).pop(snapshot_path, None)
        self._local.sources = None

    def _command(self, command: VerificationCommand, prepared: _PrivateRuntime) -> tuple[str, ...]:
        executable = command.argv[0]
        is_python = executable == "{python}"
        if not is_python and Path(executable).is_absolute():
            is_python = Path(executable).resolve() == Path(sys.executable).resolve()
        if is_python:
            arguments = ("-B", *command.argv[1:])
            if self._python_module(arguments) in {"pytest", "pytest.__main__"}:
                # Python 3.14 的 pdb 会导入 asyncio/_overlapped；pytest 默认 debugging
                # 插件即使未请求 --pdb 也会导入 pdb。LPAC 正确拒绝相关网络能力，
                # 因此用 pytest 官方插件开关禁用非必需的交互调试插件，不放宽隔离。
                # 控制参数置于请求参数之后，候选不能从最终实际 argv 中移除该约束。
                arguments = (*arguments, "-p", "no:debugging")
            return (str(prepared.python), *arguments)
        binary = _physical_path(Path(executable), directory=False, allow_hardlinks=True)
        for index, root in enumerate(self.readonly_tools):
            if root in binary.parents:
                relative = f"tools/{index}/" + binary.relative_to(root).as_posix()
                if relative in prepared.files:
                    return (str(prepared.python.parent.parent / relative), *command.argv[1:])
        raise LegacyVerificationError(
            "untrusted_executable", "验证程序必须是可信 Python 或显式只读工具中的绝对 EXE",
        )

    @staticmethod
    def _python_module_invocation(
        arguments: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str, tuple[str, ...]] | None:
        """只识别支持的 CPython argv，不让合并/未知选项绕过工具入口检查。"""
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == "-m":
                if index + 1 >= len(arguments):
                    return None
                return arguments[:index], arguments[index + 1], arguments[index + 2:]
            if argument.startswith("-m"):
                return arguments[:index], argument[2:], arguments[index + 1:]
            if argument == "-c" or argument == "--" or not argument.startswith("-"):
                return None  # 明确的候选脚本/内联命令，不假称它是已安装测试工具。
            if argument in {"-V", "--version", "-h", "--help"}:
                return None  # 解释器会直接结束，后续 -m 不是实际执行的工具。
            if argument in {"-W", "-X"}:
                index += 2
                continue
            if (argument in {"-B", "-S", "-I", "-E", "-s", "-P", "-q", "-u",
                             "-O", "-OO", "-v"}
                    or argument.startswith(("-W", "-X"))):
                index += 1
                continue
            raise LegacyVerificationError("unsupported_python_argv", "验证 Python 不支持该选项组合")
        return None

    @staticmethod
    def _python_module(arguments: tuple[str, ...]) -> str | None:
        invocation = WindowsIsolatedCommandPort._python_module_invocation(arguments)
        return invocation[1] if invocation is not None else None

    @staticmethod
    def _pytest_shim_launch(
        argv: tuple[str, ...], *, task_root: Path, run_id: str,
    ) -> tuple[tuple[str, ...], Path] | None:
        invocation = WindowsIsolatedCommandPort._python_module_invocation(argv[1:])
        if invocation is None or invocation[1] not in {"pytest", "pytest.__main__"}:
            return None
        interpreter, module, arguments = invocation
        relative = (
            Path(".ai-dev-os-worker") / f"{run_id}-e1" / "tmp" / _PYTEST_DEVNULL_NAME
        )
        return (
            (
                argv[0], *interpreter, "-c", _PYTEST_DEVNULL_SHIM, module,
                str(relative), str(len(interpreter)), *arguments,
            ),
            task_root / relative,
        )

    @staticmethod
    def _pytest_devnull_relative(path: Path, task_root: Path) -> Path:
        try:
            relative = path.relative_to(task_root)
        except ValueError as exc:
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull 路径逃逸 Task",
            ) from exc
        if (
            len(relative.parts) != 4
            or relative.parts[0] != ".ai-dev-os-worker"
            or not relative.parts[1].endswith("-e1")
            or relative.parts[2:] != ("tmp", _PYTEST_DEVNULL_NAME)
        ):
            raise LegacyVerificationError("cleanup_unconfirmed", "pytest 私有 devnull 路径异常")
        return relative

    @staticmethod
    def _handle_file_state(handle: Any, path: Path) -> dict[str, Any]:
        api = _win32()
        info = api.ByHandleFileInformation()
        if not api.kernel.GetFileInformationByHandle(handle, api.ctypes.byref(info)):
            raise OSError(api.ctypes.get_last_error(), "读取 pytest 私有 devnull 句柄失败")
        volume_serial, file_id, attributes = _handle_identity(handle, path, str(path))
        return {
            "volume_serial": volume_serial,
            "file_id": file_id,
            "attributes": attributes,
            "size": (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow),
            "nlink": int(info.nNumberOfLinks),
        }

    @classmethod
    def _hold_pytest_devnull(cls, path: Path, *, task_root: Path) -> _HeldPytestDevnull:
        """可信控制器独占创建并持有禁止 delete-share 的不可替换空文件。"""

        cls._pytest_devnull_relative(path, task_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if _physical_path(task_root) != task_root or _physical_path(path.parent) != path.parent:
            raise LegacyVerificationError("cleanup_unconfirmed", "pytest 私有 devnull 创建路径身份漂移")
        api = _win32()
        # GENERIC_READ | GENERIC_WRITE；只共享 read/write，明确不共享 delete/rename。
        handle = api.kernel.CreateFileW(str(path), 0xC0000000, 0x3, None, 1, 0x80, None)
        if handle in (None, api.ctypes.c_void_p(-1).value):
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull 无法独占创建",
            )
        try:
            state = cls._handle_file_state(handle, path)
            final = _final_handle_path(handle, path)
            if (
                state["attributes"] & (0x10 | 0x400)
                or state["size"] != 0
                or state["nlink"] != 1
                or os.path.normcase(str(final)) != os.path.normcase(str(path))
            ):
                raise LegacyVerificationError(
                    "cleanup_unconfirmed", "pytest 私有 devnull 初始物理身份异常",
                )
            return _HeldPytestDevnull(
                path, task_root, handle, state["volume_serial"], state["file_id"],
                state["attributes"],
            )
        except BaseException:
            api.kernel.CloseHandle(handle)
            raise

    @classmethod
    def _cleanup_pytest_devnull(cls, held: _HeldPytestDevnull) -> dict[str, Any]:
        """Job 回收后以持有句柄核对同一对象，关闭锁后删除精确私有路径。"""

        path, task_root = held.path, held.task_root
        try:
            relative = cls._pytest_devnull_relative(path, task_root)
        except LegacyVerificationError:
            held.close()
            raise
        try:
            physical_task = _physical_path(task_root)
        except (OSError, WorkerIsolationError) as exc:
            held.close()
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull Task 根物理身份无法确认",
            ) from exc
        if physical_task != task_root:
            held.close()
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull Task 根身份漂移",
            )
        try:
            physical_devnull = _physical_path(path, directory=False)
        except (OSError, WorkerIsolationError) as exc:
            held.close()
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull 祖先物理身份无法确认",
            ) from exc
        if physical_devnull != path:
            held.close()
            raise LegacyVerificationError("cleanup_unconfirmed", "pytest 私有 devnull 路径身份漂移")
        path_handle = None
        try:
            held_state = cls._handle_file_state(held.handle, path)
            path_handle = _open_proof_handle(path, reparse=False)
            path_state = cls._handle_file_state(path_handle, path)
            final = _final_handle_path(path_handle, path)
        except (OSError, WorkerIsolationError) as exc:
            held.close()
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull 无法核对",
            ) from exc
        finally:
            if path_handle is not None:
                _win32().kernel.CloseHandle(path_handle)
        expected_identity = (held.volume_serial, held.file_id, held.attributes)
        if (
            (held_state["volume_serial"], held_state["file_id"], held_state["attributes"])
            != expected_identity
            or (path_state["volume_serial"], path_state["file_id"], path_state["attributes"])
            != expected_identity
            or held_state["nlink"] != 1
            or path_state["nlink"] != 1
            or os.path.normcase(str(final)) != os.path.normcase(str(path))
        ):
            held.close()
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull 物理身份发生变化",
            )
        evidence = {
            "relative_task_path": relative.as_posix(),
            "volume_serial": held.volume_serial,
            "file_id": held.file_id,
            "object_type": "regular_file",
            "initial_size": 0,
            "initial_nlink": 1,
            "st_nlink": held_state["nlink"],
            "post_execution_size": held_state["size"],
            "delete_share_denied_during_execution": True,
        }
        held.close()
        try:
            path.unlink()
            path.lstat()
        except FileNotFoundError:
            evidence["cleaned"] = True
        except OSError as exc:
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "pytest 私有 devnull 删除未确认",
            ) from exc
        else:
            raise LegacyVerificationError("cleanup_unconfirmed", "pytest 私有 devnull 删除未生效")
        return evidence

    @staticmethod
    def _trusted_tool_identity(prepared: _PrivateRuntime, module: str | None) -> dict[str, Any]:
        """复用已安装分发元数据；只做已知工具来源/命名冲突门禁，不替换 Python loader。"""
        if module is None:
            return {}
        requested_module = module
        module = module.split(".", 1)[0]
        if module not in {"pytest", "ruff", "mypy", "_pytest", "mypyc"}:
            return {}
        if (module in {"_pytest", "mypyc"}
                or requested_module not in {module, f"{module}.__main__"}):
            raise LegacyVerificationError("unsupported_python_tool_entry", "验证工具必须使用明确的公开入口")
        # packaging 是测试工具的既有依赖，不能变成正常 CLI import 的新前置条件。
        try:
            from packaging.requirements import Requirement
            from packaging.utils import canonicalize_name
        except ImportError as exc:
            raise LegacyVerificationError(
                "tool_environment_unavailable", "可信测试工具缺少既有 packaging 元数据解析依赖",
            ) from exc

        site_packages = prepared.python.parent / "Lib" / "site-packages"
        # prepared.files 统一相对 runtime_root，不能误用相对 python/ 的 RECORD 根。
        site_prefix = site_packages.relative_to(prepared.python.parent.parent).as_posix() + "/"
        distributions: dict[str, metadata.PathDistribution] = {}
        for path in sorted(site_packages.glob("*.dist-info")):
            installed_distribution = metadata.PathDistribution(path)
            name = installed_distribution.metadata["Name"]
            if not name:
                raise LegacyVerificationError("tool_environment_unavailable", "工具分发元数据缺少名称")
            key = canonicalize_name(name)
            if key in distributions:
                raise LegacyVerificationError("tool_environment_unavailable", "可信工具分发名称冲突")
            distributions[key] = installed_distribution

        pending = [(module, frozenset[str]())]
        if module == "pytest":
            # pytest 原生会发现 pytest11 插件；xdist/execnet 同样按实际已装 metadata 绑定。
            pending.extend(
                (name, frozenset[str]()) for name, distribution in distributions.items()
                if any(entry.group == "pytest11" for entry in distribution.entry_points)
            )
        visited: dict[str, str] = {}
        checked: set[tuple[str, frozenset[str]]] = set()
        imports: set[str] = set()
        tool_files: set[str] = set()
        while pending:
            distribution_name, extras = pending.pop()
            name = canonicalize_name(distribution_name)
            if (name, extras) in checked:
                continue
            checked.add((name, extras))
            distribution = distributions.get(name)
            recorded = distribution.files if distribution is not None else None
            if distribution is None or recorded is None:
                raise LegacyVerificationError(
                    "tool_environment_unavailable", f"可信测试工具缺少完整安装记录：{name}",
                )
            visited[name] = distribution.version
            owned = {
                site_prefix + path.as_posix() for path in recorded
                if ".." not in path.parts and not path.is_absolute()
            } & prepared.files.keys()
            if name == module:
                tool_files.update(owned)
            # RECORD 兼容没有 top_level.txt 的 wheel，并包含 mypy 哈希命名原生模块。
            for relative in owned:
                part = relative.removeprefix(site_prefix).split("/", 1)[0]
                if "/" not in relative.removeprefix(site_prefix):
                    part = part.split(".", 1)[0]
                if (any(relative.endswith(suffix) for suffix in machinery.all_suffixes())
                        and re.fullmatch(r"[A-Za-z0-9_]+", part)):
                    imports.add(part.casefold())
            for declaration in distribution.requires or ():
                requirement = Requirement(declaration)
                if requirement.marker is None or any(
                    requirement.marker.evaluate({"extra": extra}) for extra in ("", *extras)
                ):
                    pending.append((requirement.name, frozenset(requirement.extras)))

        entry_options = {
            f"{site_prefix}{module}/__main__.py", f"{site_prefix}{module}.py",
        }
        entries = sorted(entry_options & tool_files)
        if not entries or module not in imports:
            raise LegacyVerificationError("tool_environment_unavailable", "已安装工具入口未绑定可信文件")
        # pytest 会向 sys.path 加入测试目录/配置 pythonpath；因此不只检查 root/src。
        # 明确拒绝候选中的工具保留名，业务包（含同名旧安装业务包）仍 candidate-first。
        for current, directories, files in os.walk(prepared.snapshot_path, followlinks=False):
            for name in (*directories, *files):
                item = Path(current) / name
                candidate_name = name if name in directories else name.split(".", 1)[0]
                importable = name in directories or any(
                    name.casefold().endswith(suffix.casefold()) for suffix in machinery.all_suffixes()
                )
                if importable and candidate_name.casefold() in imports:
                    raise LegacyVerificationError(
                        "tool_import_conflict", "候选包含与可信测试工具/内部依赖冲突的模块或包",
                        details={"tool": module, "candidate_path": str(item), "import_name": candidate_name},
                    )
        return {
            "module": requested_module, "distributions": visited, "reserved_import_names": sorted(imports),
            "entry_files": {name: prepared.files[name] for name in entries},
        }

    def run(
        self, command: VerificationCommand, *, snapshot_path: Path,
        protected_roots: tuple[Path, ...], run_id: str,
    ) -> CommandExecution:
        command.validate()
        if not _is_windows_lpac_platform():
            raise LegacyVerificationError(
                "isolation_unavailable", "当前平台尚无可信验证隔离后端，拒绝直接执行候选代码",
            )
        started = time.monotonic()
        prepared, reused = self._prepare(snapshot_path)
        python = prepared.python
        argv = self._command(command, prepared)
        tool_identity = self._trusted_tool_identity(prepared, self._python_module(argv[1:])) if (
            argv[0] == str(python)
        ) else {}
        # launcher 在 Task 私有域内绑定物理 cwd，直接启动真实工具。pytest 默认 fd capture
        # 会打开 Windows NUL，而 LPAC 正确拒绝设备；只对该可信模块使用同进程 runpy shim，
        # 将 os.devnull 绑定到 run-specific TMP 的物理空文件，绝不再启动第二个解释器。
        # 无 shell 拼接，也不在控制器进程中加载候选模块。
        launch_argv = argv
        pytest_devnull: Path | None = None
        pytest_launch = self._pytest_shim_launch(
            argv, task_root=snapshot_path.parent, run_id=run_id,
        )
        if pytest_launch is not None:
            launch_argv, pytest_devnull = pytest_launch
        source_roots = (
            *self.python_dependencies, *(source.parent for source in self.python_scripts),
            *self.readonly_tools, Path(sys.base_prefix).resolve(strict=True),
            Path(sys.executable).resolve(strict=True).parent,
        )
        spec = WorkerIsolationSpec(
            snapshot_path.parent, tuple(dict.fromkeys((*protected_roots, *source_roots))),
            (), run_id, 1,
        )
        capability = self.launcher.probe(spec)
        if not capability.supported:
            raise LegacyVerificationError("isolation_unavailable", capability.reason)
        pytest_lock = (
            self._hold_pytest_devnull(pytest_devnull, task_root=snapshot_path.parent)
            if pytest_devnull is not None else None
        )
        try:
            process = self.launcher.launch(
                spec, launch_argv, working_directory=snapshot_path,
            )
        except BaseException:
            if pytest_lock is not None:
                self._cleanup_pytest_devnull(pytest_lock)
            raise
        tree: _ProcessTree | None = None
        readers: list[threading.Thread] = []
        captures: list[_OutputDigest] = []
        devnull_evidence: dict[str, Any] | None = None
        timed_out = False
        returncode = 127
        try:
            for stream in (process.stdout, process.stderr):
                if not isinstance(stream, io.TextIOWrapper):
                    raise LegacyVerificationError("invalid_output", "验证管道缺少原始字节流")
                capture = _OutputDigest(cast(BinaryIO, stream.buffer))
                captures.append(capture)
                readers.append(threading.Thread(target=capture.drain, daemon=True))
            if process.stdin is not None:
                process.stdin.close()
            tree = _ProcessTree(process)
            for reader in readers:
                reader.start()
            try:
                returncode = process.wait(timeout=command.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = 124
        finally:
            # 先确认整棵树终止，之后才能等待 EOF、撤销 ACL 和清理副本。
            # 任一步失败直接抛出；不得以 leader 已退出推断后代也已经停止。
            if tree is not None:
                tree.kill()
            else:
                process.kill()
                process.wait(timeout=5)
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=5)
                    if reader.is_alive():
                        raise LegacyVerificationError("cleanup_unconfirmed", "验证输出管道未关闭")
            process.close()
            if pytest_lock is not None:
                devnull_evidence = self._cleanup_pytest_devnull(pytest_lock)
        if any(capture.error is not None for capture in captures):
            raise LegacyVerificationError("output_unconfirmed", "验证输出读取失败，不能生成摘要")
        if process.cleanup_evidence != {
            "task_sid_removed": True, "profile_deleted_before_resume": True,
        }:
            raise LegacyVerificationError("cleanup_unconfirmed", "验证进程权限域未完整回收")
        pytest_shim_evidence: dict[str, Any] | None = None
        if pytest_devnull is not None:
            if devnull_evidence is None:
                raise LegacyVerificationError(
                    "cleanup_unconfirmed", "pytest 私有 devnull 清理证据缺失",
                )
            pytest_shim_evidence = {
                "version": _PYTEST_DEVNULL_SHIM_VERSION,
                "sha256": _PYTEST_DEVNULL_SHIM_SHA256,
                "devnull": devnull_evidence,
            }
        # Job/权限域已回收之后，完整内容集合必须不变，才能在同一 snapshot 复用。
        # 最后由 adapter 一次回收整个专用临时域；异常保留诊断而非冒充已清理。
        try:
            self._assert_private_runtime(prepared, snapshot_path)
        except LegacyVerificationError as exc:
            exc.details["command_output"] = {
                "actual_argv": list(argv), "returncode": returncode,
                "stdout_bytes": captures[0].size, "stderr_bytes": captures[1].size,
                "stdout_sha256": captures[0].digest.hexdigest(),
                "stderr_sha256": captures[1].digest.hexdigest(),
                "stdout_preview_hex": captures[0].preview.hex(),
                "stderr_preview_hex": captures[1].preview.hex(),
                "stdout_suffix_preview_hex": captures[0].suffix_preview.hex(),
                "stderr_suffix_preview_hex": captures[1].suffix_preview.hex(),
                "stdout_preview_complete": captures[0].preview_complete,
                "stderr_preview_complete": captures[1].preview_complete,
                "output_preview_limit_bytes": _OUTPUT_PREVIEW_LIMIT,
            }
            raise
        return CommandExecution(
            returncode, captures[0].digest.hexdigest(), captures[1].digest.hexdigest(),
            time.monotonic() - started, True,
            {
                "actual_argv": list(argv),
                "actual_argv_fingerprint": fingerprint(list(argv)),
                "launch_argv": list(launch_argv),
                **({"pytest_devnull_shim": pytest_shim_evidence}
                   if pytest_shim_evidence is not None else {}),
                "working_directory": str(snapshot_path),
                "python_dependencies_sha256": prepared.environment["python_dependencies_sha256"],
                "python_runtime_sha256": prepared.environment["python_runtime_sha256"],
                "readonly_tools_sha256": prepared.environment["readonly_tools_sha256"],
                "private_runtime_sha256": fingerprint(prepared.files),
                "executed_binary_sha256": prepared.files[
                    Path(argv[0]).relative_to(python.parent.parent).as_posix()
                ],
                "runtime_reused": reused,
                "trusted_python_tool": tool_identity,
                "stdout_bytes": captures[0].size,
                "stderr_bytes": captures[1].size,
                "stdout_preview_hex": captures[0].preview.hex(),
                "stderr_preview_hex": captures[1].preview.hex(),
                "stdout_suffix_preview_hex": captures[0].suffix_preview.hex(),
                "stderr_suffix_preview_hex": captures[1].suffix_preview.hex(),
                "stdout_preview_complete": captures[0].preview_complete,
                "stderr_preview_complete": captures[1].preview_complete,
                "output_preview_limit_bytes": _OUTPUT_PREVIEW_LIMIT,
                "timed_out": timed_out,
                "process_returncode": process.returncode,
                "isolation": copy.deepcopy(capability.evidence),
                "launch_isolation": copy.deepcopy(process.isolation_evidence),
                "cleanup": copy.deepcopy(process.cleanup_evidence),
            },
        )


class LegacyVerificationAdapter:
    """将实际受控命令结果包装成 Phase 2 冻结的 VerificationReceiptEnvelope。

environment 来自可信执行器，而不是候选配置中的环境变量。每个执行回合只导出
固定 commit 的纯 blob 副本，无共享 .git、canonical Workspace 或 Gate 写权。
非零退出码产生真实 FAIL 回执；未知隔离/清理/候选状态不产生成功回执。
"""

    provider_id = "legacy-verification-adapter"
    provider_version = "1"

    def __init__(
        self, *, protected_roots: tuple[Path, ...], environment: dict[str, str] | None = None,
        readonly_tools: tuple[Path, ...] = (), command_port: VerificationCommandPort | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not protected_roots:
            raise LegacyVerificationError("invalid_protection", "验证必须声明控制面保护根")
        self.protected_roots = tuple(_physical_path(path) for path in protected_roots)
        self.command_port = command_port or WindowsIsolatedCommandPort(readonly_tools=readonly_tools)
        self.clock = clock
        actual = self.command_port.environment()
        self.environment = copy.deepcopy(actual if environment is None else environment)
        if self.environment != actual:
            raise LegacyVerificationError("environment_mismatch", "配置环境与可信执行器实际环境不同")

    @staticmethod
    def _candidate(git: TrustedGit, workspace_path: Path) -> tuple[str, str]:
        if git.run("status", "--porcelain=v1", "--untracked-files=all", cwd=workspace_path):
            raise LegacyVerificationError("dirty_candidate", "候选工作树不干净，不能绑定验证证据")
        sha = git.resolve("HEAD", cwd=workspace_path)
        tree = git.run("rev-parse", "HEAD^{tree}", cwd=workspace_path).strip()
        return sha, tree

    @staticmethod
    def _file_identity(path: Path) -> tuple[str, int]:
        physical = _physical_path(path, directory=False)
        with physical.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        return digest, physical.stat().st_mode & 0o111

    @classmethod
    def _snapshot_manifest(cls, snapshot: Path) -> dict[str, tuple[str, int]]:
        # export_tree 只导出普通 tracked blob，因此这里不会加载候选模块或配置。
        return {
            path.relative_to(snapshot).as_posix(): cls._file_identity(path)
            for path in snapshot.rglob("*") if path.is_file()
        }

    @classmethod
    def _assert_snapshot(cls, snapshot: Path, manifest: dict[str, tuple[str, int]]) -> None:
        for name, identity in manifest.items():
            try:
                current = cls._file_identity(snapshot / name)
            except (OSError, WorkerIsolationError) as exc:
                raise LegacyVerificationError(
                    "snapshot_changed", "验证副本中的 tracked 源码身份发生变化",
                ) from exc
            if current != identity:
                raise LegacyVerificationError("snapshot_changed", "验证命令改写了 tracked 源码")

    @staticmethod
    def _cleanup_snapshot(temporary_root: Path, snapshot: Path) -> None:
        # 只删除当前 run 创建的精确临时目录，不接受外部清理目标或根路径替换。
        physical = _physical_path(temporary_root)
        if physical != temporary_root or snapshot.parent != temporary_root:
            raise LegacyVerificationError("cleanup_unconfirmed", "验证副本清理目标身份改变")
        shutil.rmtree(physical)

    @classmethod
    def _private_git_candidate(
        cls, snapshot: Path, git: TrustedGit, *, object_format: str,
    ) -> tuple[TrustedGit, Path, Path]:
        """为纯 blob candidate 建立一次性私有 Git 对象库，不复制或暴露共享 .git。"""

        marker = snapshot / ".git"
        git_dir = snapshot.parent / f"git-static-{uuid4().hex}"
        if marker.exists() or git_dir.exists():
            raise LegacyVerificationError("snapshot_changed", "验证副本意外包含 Git 元数据")
        environment = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
        )
        command = [
            git.executable, "--no-optional-locks", "-c", "core.hooksPath=" + os.devnull,
            "-c", "init.templateDir=", "init", "--quiet",
            f"--object-format={object_format}",
            f"--separate-git-dir={git_dir}", str(snapshot),
        ]
        try:
            initialized = subprocess.run(
                command, cwd=snapshot.parent, env=environment, capture_output=True,
                check=False, timeout=30,
            )
            if initialized.returncode != 0:
                detail = (initialized.stderr or initialized.stdout)[:4096].decode(
                    "utf-8", errors="replace",
                )
                raise LegacyVerificationError(
                    "git_static_check_unavailable", f"无法建立私有 Git 检查副本：{detail}",
                )
            private = TrustedGit(snapshot)
        except (OSError, subprocess.TimeoutExpired) as exc:
            original = LegacyVerificationError(
                "git_static_check_unavailable", f"无法建立私有 Git 检查副本：{exc}",
            )
            original.__cause__ = exc
            cls._finish_private_git_cleanup(snapshot, marker, git_dir, original=original)
            raise original from exc
        except Exception as exc:
            cls._finish_private_git_cleanup(snapshot, marker, git_dir, original=exc)
            raise
        return private, marker, git_dir

    @staticmethod
    def _remove_private_git(snapshot: Path, marker: Path, git_dir: Path) -> None:
        """幂等尽力清理精确私有 Git 路径；一项失败不阻断另一项。"""

        if (marker != snapshot / ".git" or git_dir.parent != snapshot.parent
                or not git_dir.name.startswith("git-static-")):
            raise LegacyVerificationError("cleanup_unconfirmed", "私有 Git 清理目标身份异常")
        # 先确认共同父域，不能因错误参数把尽力清理扩到 verification root 之外。
        try:
            physical_snapshot = _physical_path(snapshot)
        except (OSError, WorkerIsolationError) as exc:
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "无法确认私有 Git 所属 verification 副本",
            ) from exc
        if physical_snapshot != snapshot or git_dir.parent != physical_snapshot.parent:
            raise LegacyVerificationError("cleanup_unconfirmed", "私有 Git 清理目标越出 verification 域")

        failures: list[str] = []
        try:
            marker_info = marker.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(f"marker_stat:{exc}")
        else:
            if (not stat.S_ISREG(marker_info.st_mode) or stat.S_ISLNK(marker_info.st_mode)
                    or getattr(marker_info, "st_file_attributes", 0) & 0x400):
                failures.append("marker_identity:不是物理普通文件")
            else:
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    failures.append(f"marker_unlink:{exc}")

        try:
            git_dir_info = git_dir.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(f"git_dir_stat:{exc}")
        else:
            safe_tree = stat.S_ISDIR(git_dir_info.st_mode) and not (
                stat.S_ISLNK(git_dir_info.st_mode)
                or getattr(git_dir_info, "st_file_attributes", 0) & 0x400
            )
            if not safe_tree:
                failures.append("git_dir_identity:不是物理目录")
            else:
                try:
                    physical = _physical_path(git_dir)
                    for current, directories, files in os.walk(physical, followlinks=False):
                        for name in directories:
                            _physical_path(Path(current) / name)
                        for name in files:
                            item = _physical_path(Path(current) / name, directory=False)
                            try:
                                item.chmod(stat.S_IREAD | stat.S_IWRITE)
                            except OSError as exc:
                                failures.append(f"git_file_chmod:{item}:{exc}")
                    try:
                        shutil.rmtree(physical)
                    except FileNotFoundError:
                        pass
                except (OSError, WorkerIsolationError) as exc:
                    failures.append(f"git_dir_remove:{exc}")

        for label, path in (("marker", marker), ("git_dir", git_dir)):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures.append(f"{label}_final_stat:{exc}")
            else:
                failures.append(f"{label}_retained")
        if failures:
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "私有 Git 检查副本未完整清理",
                details={"cleanup_failures": failures},
            )

    @classmethod
    def _finish_private_git_cleanup(
        cls, snapshot: Path, marker: Path, git_dir: Path,
        *, original: Exception | None = None,
    ) -> None:
        """清理失败优先 fail-closed，同时结构化保留原始操作失败。"""

        try:
            cls._remove_private_git(snapshot, marker, git_dir)
        except LegacyVerificationError as cleanup:
            if original is None:
                raise
            raise LegacyVerificationError(
                "cleanup_unconfirmed", "私有 Git 操作失败且临时对象未确认清理",
                details={
                    "original_failure": {
                        "type": type(original).__name__,
                        "code": getattr(original, "code", None),
                        "message": str(original),
                    },
                    "cleanup_failure": {
                        "message": str(cleanup), **copy.deepcopy(cleanup.details),
                    },
                },
            ) from original

    @classmethod
    def _git_static_check(
        cls, command: VerificationCommand, git: TrustedGit, sha: str, snapshot: Path,
    ) -> CommandExecution | None:
        """V1 的精确 git diff --check 映射成固定候选对象的原生只读检查。

        固定 commit 已导出为独立 blob 副本；再用一次性私有对象库建立等价 root commit。
        不接纳 git 任意参数，不把共享 .git 或原生 Git 子进程交给候选测试代码。
        """
        if command.argv[1:] != ("diff", "--check"):
            return None
        executable = command.argv[0]
        if executable != "git" and (
            not Path(executable).is_absolute()
            or Path(executable).resolve() != Path(git.executable).resolve()
        ):
            return None
        started = time.monotonic()
        entries = git.tree_entries(sha)
        private_git, marker, git_dir = cls._private_git_candidate(
            snapshot, git, object_format="sha256" if len(sha) == 64 else "sha1",
        )
        try:
            names = tuple(entries)
            object_ids = private_git.run_bytes(
                "hash-object", "-w", "--no-filters", "--stdin-paths", cwd=snapshot,
                input=("\n".join(names) + ("\n" if names else "")).encode("utf-8"),
            ).decode("ascii").splitlines()
            expected_ids = [oid for _mode, oid in entries.values()]
            if object_ids != expected_ids:
                raise LegacyVerificationError(
                    "snapshot_changed", "私有 Git 归集的 blob 身份与固定 candidate 不同",
                )
            index = b"".join(
                f"{mode} {oid}\t{name}\n".encode()
                for (name, (mode, _expected)), oid in zip(
                    entries.items(), object_ids, strict=True,
                )
            )
            private_git.run_bytes("update-index", "--index-info", input=index)
            tree = private_git.run("write-tree")
            original_tree = git.run("rev-parse", f"{sha}^{{tree}}")
            if tree != original_tree:
                raise LegacyVerificationError(
                    "snapshot_changed", "私有 Git tree 身份与固定 candidate 不同",
                )
            commit = private_git.run(
                "commit-tree", tree, input=b"private static-check candidate\n",
                extra_env={
                    "GIT_AUTHOR_NAME": "AI Dev OS Verification",
                    "GIT_AUTHOR_EMAIL": "verification@example.invalid",
                    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                    "GIT_COMMITTER_NAME": "AI Dev OS Verification",
                    "GIT_COMMITTER_EMAIL": "verification@example.invalid",
                    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
                },
            )
            completed = private_git.checked_diff(
                commit, timeout_seconds=command.timeout_seconds,
            )
        except Exception as exc:
            cls._finish_private_git_cleanup(snapshot, marker, git_dir, original=exc)
            raise
        else:
            cls._finish_private_git_cleanup(snapshot, marker, git_dir)
        actual_argv = list(completed.args)
        return CommandExecution(
            completed.returncode, hashlib.sha256(completed.stdout).hexdigest(),
            hashlib.sha256(completed.stderr).hexdigest(), time.monotonic() - started, True,
            {
                "actual_argv": actual_argv,
                "actual_argv_fingerprint": fingerprint(actual_argv),
                "execution_boundary": "trusted-git-static-candidate-diff",
                "candidate_sha": sha,
                "private_candidate_tree": tree,
                "private_candidate_commit": commit,
                "working_directory": str(snapshot),
                **_completed_output_evidence(completed.stdout, completed.stderr),
                "process_returncode": completed.returncode,
                "timed_out": False,
            },
        )

    def execute(
        self, plan: VerificationPlan, *, workspace_path: Path,
    ) -> VerificationReceiptEnvelope:
        plan = VerificationPlan.from_dict(plan.to_dict())
        plan.validate()
        if (plan.environment != self.environment
                or self.command_port.environment() != self.environment):
            raise LegacyVerificationError("environment_mismatch", "验证计划与实际执行环境不匹配")
        workspace = _physical_path(workspace_path)
        git = TrustedGit(workspace)
        expected = (plan.candidate_sha, plan.candidate_tree)
        if self._candidate(git, workspace) != expected:
            raise LegacyVerificationError("stale_verification", "验证计划不是当前候选提交和 tree")
        started_at = datetime.fromtimestamp(self.clock(), UTC).isoformat()
        run_id = f"verification-{uuid4().hex}"
        temporary_root = Path(tempfile.mkdtemp(prefix="ai-dev-os-verification-")).resolve()
        snapshot = temporary_root / "candidate"
        results: list[VerificationCommandResult] = []
        evidence: list[dict[str, Any]] = []
        # 默认把源 worktree 和共享 Git 也纳入保护域，不能仅依赖调用者漏报的根。
        protected = tuple(dict.fromkeys((
            *self.protected_roots, workspace, git.common_dir, Path(__file__).resolve().parents[1],
        )))
        try:
            git.export_tree(plan.candidate_sha, snapshot)
            manifest = self._snapshot_manifest(snapshot)
            if self._candidate(git, workspace) != expected:
                raise LegacyVerificationError("stale_verification", "导出期间候选发生变化")
            for index, command in enumerate(plan.commands):
                execution = self._git_static_check(
                    command, git, plan.candidate_sha, snapshot,
                )
                if execution is None:
                    execution = self.command_port.run(
                        command, snapshot_path=snapshot, protected_roots=protected,
                        run_id=f"{run_id}-{index}",
                    )
                if execution.cleanup_confirmed is not True:
                    raise LegacyVerificationError("cleanup_unconfirmed", "命令未证明隔离域已安全回收")
                result = VerificationCommandResult(
                    command.command_id, execution.returncode, execution.stdout_sha256,
                    execution.stderr_sha256, execution.duration_seconds,
                )
                results.append(result)
                evidence.append(copy.deepcopy(execution.evidence))
                self._assert_snapshot(snapshot, manifest)
                if self.command_port.environment() != self.environment:
                    raise LegacyVerificationError("environment_mismatch", "命令执行期间环境发生变化")
                if self._candidate(git, workspace) != expected:
                    raise LegacyVerificationError("stale_verification", "命令执行期间候选发生变化")
            self._cleanup_snapshot(temporary_root, snapshot)
            if self._candidate(git, workspace) != expected:
                raise LegacyVerificationError("stale_verification", "清理期间候选发生变化")
            if self.command_port.environment() != self.environment:
                raise LegacyVerificationError("environment_mismatch", "清理期间执行环境发生变化")
            return VerificationReceiptEnvelope(
                run_id, plan.plan_id, plan.requirement_id, plan.task_id,
                plan.candidate_sha, plan.candidate_tree, copy.deepcopy(self.environment),
                plan.commands_fingerprint, tuple(results), started_at,
                datetime.fromtimestamp(self.clock(), UTC).isoformat(),
                self.provider_id, self.provider_version,
                extra={
                    "commands": [command.to_dict() for command in plan.commands],
                    "execution_evidence": evidence,
                    "source_workspace": str(workspace),
                    "snapshot_kind": "git-tracked-blobs-without-shared-git",
                    "snapshot_manifest_fingerprint": fingerprint(manifest),
                    "snapshot_cleaned": True,
                },
            )
        except Exception as exc:
            details = {
                "snapshot_path": str(snapshot),
                "completed_results": [result.to_dict() for result in results],
                "execution_evidence": evidence,
            }
            if isinstance(exc, LegacyVerificationError):
                exc.details.update(details)
                raise
            code = exc.code if isinstance(exc, (PolicyError, WorkerIsolationError)) else "execution_unknown"
            raise LegacyVerificationError(code, f"验证未能可信结束：{exc}", details=details) from exc
        finally:
            if isinstance(self.command_port, WindowsIsolatedCommandPort):
                self.command_port.release(snapshot)
