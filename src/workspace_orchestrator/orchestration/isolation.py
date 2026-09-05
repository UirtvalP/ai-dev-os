"""Worker 的进程隔离边界；未证明可强制执行的配置一律拒绝启动。

Provider 的 read-only/workspace-write 只是请求，不是 OS 隔离证明。这里的能力
只能由可信控制面探测，不能从 Worker 事件、仓库配置或环境变量接受授权。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _win32() -> Any:
    """绑定稳定公开的 Win32 API；不调用 Codex setup 或实验性 SandboxEngine。"""

    if sys.platform != "win32":
        raise WorkerIsolationError("platform_unavailable", "AppContainer 仅支持 Windows")
    import ctypes
    from ctypes import wintypes as wt
    from types import SimpleNamespace

    class StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("lpReserved", wt.LPWSTR),
            ("lpDesktop", wt.LPWSTR),
            ("lpTitle", wt.LPWSTR),
            ("dwX", wt.DWORD),
            ("dwY", wt.DWORD),
            ("dwXSize", wt.DWORD),
            ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD),
            ("dwYCountChars", wt.DWORD),
            ("dwFillAttribute", wt.DWORD),
            ("dwFlags", wt.DWORD),
            ("wShowWindow", wt.WORD),
            ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wt.HANDLE),
            ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wt.HANDLE),
            ("hThread", wt.HANDLE),
            ("dwProcessId", wt.DWORD),
            ("dwThreadId", wt.DWORD),
        ]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wt.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wt.BOOL),
        ]

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

    class SecurityCapabilities(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", ctypes.c_void_p),
            ("Capabilities", ctypes.POINTER(SidAndAttributes)),
            ("CapabilityCount", wt.DWORD),
            ("Reserved", wt.DWORD),
        ]

    dll = ctypes.WinDLL
    kernel, userenv, advapi = (
        dll(name, use_last_error=True) for name in ("kernel32", "userenv", "advapi32")
    )
    kernelbase = dll("kernelbase", use_last_error=True)
    ole32 = dll("ole32", use_last_error=True)
    void = ctypes.c_void_p
    signatures = (
        (
            kernel,
            "InitializeProcThreadAttributeList",
            [void, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)],
            wt.BOOL,
        ),
        (
            kernel,
            "UpdateProcThreadAttribute",
            [void, wt.DWORD, ctypes.c_size_t, void, ctypes.c_size_t, void, void],
            wt.BOOL,
        ),
        (kernel, "DeleteProcThreadAttributeList", [void], None),
        (
            kernel,
            "CreateProcessW",
            [wt.LPCWSTR, wt.LPWSTR, void, void, wt.BOOL, wt.DWORD, void, wt.LPCWSTR, void, void],
            wt.BOOL,
        ),
        (kernel, "CreatePipe", [void, void, void, wt.DWORD], wt.BOOL),
        (kernel, "SetHandleInformation", [wt.HANDLE, wt.DWORD, wt.DWORD], wt.BOOL),
        (kernel, "CloseHandle", [wt.HANDLE], wt.BOOL),
        (kernel, "GetExitCodeProcess", [wt.HANDLE, void], wt.BOOL),
        (kernel, "WaitForSingleObject", [wt.HANDLE, wt.DWORD], wt.DWORD),
        (kernel, "TerminateProcess", [wt.HANDLE, wt.UINT], wt.BOOL),
        (kernel, "LocalFree", [void], void),
        (
            userenv,
            "CreateAppContainerProfile",
            [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, void, wt.DWORD, void],
            ctypes.c_long,
        ),
        (userenv, "DeleteAppContainerProfile", [wt.LPCWSTR], ctypes.c_long),
        (userenv, "GetAppContainerFolderPath", [wt.LPCWSTR, void], ctypes.c_long),
        (ole32, "CoTaskMemFree", [void], None),
        (advapi, "ConvertSidToStringSidW", [void, void], wt.BOOL),
        (advapi, "ConvertStringSidToSidW", [wt.LPCWSTR, void], wt.BOOL),
        (advapi, "FreeSid", [void], void),
        (kernelbase, "DeriveCapabilitySidsFromName", [wt.LPCWSTR, void, void, void, void], wt.BOOL),
        (
            advapi,
            "GetNamedSecurityInfoW",
            [wt.LPWSTR, ctypes.c_int, wt.DWORD, void, void, void, void, void],
            wt.DWORD,
        ),
        (advapi, "GetAce", [void, wt.DWORD, void], wt.BOOL),
        (advapi, "GetLengthSid", [void], wt.DWORD),
        (advapi, "SetFileSecurityW", [wt.LPCWSTR, wt.DWORD, void], wt.BOOL),
        (
            advapi,
            "ConvertSecurityDescriptorToStringSecurityDescriptorW",
            [void, wt.DWORD, wt.DWORD, void, void],
            wt.BOOL,
        ),
    )
    for library, name, args, result in signatures:
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
    return SimpleNamespace(
        ctypes=ctypes,
        wt=wt,
        kernel=kernel,
        userenv=userenv,
        advapi=advapi,
        kernelbase=kernelbase,
        ole32=ole32,
        StartupInfoEx=StartupInfoEx,
        ProcessInformation=ProcessInformation,
        SecurityAttributes=SecurityAttributes,
        SecurityCapabilities=SecurityCapabilities,
        SidAndAttributes=SidAndAttributes,
    )


class WorkerIsolationError(RuntimeError):
    """隔离不可用或路径/策略不安全；调用者不得退回普通进程。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerIsolationSpec:
    """可信 Supervisor 为一次 Task lease 生成的最小权限请求。"""

    task_root: Path
    protected_roots: tuple[Path, ...]
    readonly_tools: tuple[Path, ...]
    run_id: str
    epoch: int
    allow_network: bool = False


@dataclass(frozen=True, slots=True)
class IsolationCapability:
    """可信探测结果；不是 Worker 可提交的授权文件。"""

    supported: bool
    backend: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IsolatedLaunch:
    """可交给现有 stdio transport 的完整外层启动计划。"""

    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    policy_fingerprint: str


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _physical_path(path: Path, *, directory: bool = True, allow_hardlinks: bool = False) -> Path:
    """只接受现存物理路径，拒绝 UNC、设备路径、链接和 Windows reparse point。"""

    if not path.is_absolute() or ".." in path.parts:
        raise WorkerIsolationError("unsafe_path", "隔离路径必须是无父级跳转的绝对路径")
    if os.name == "nt" and str(path).startswith(("\\\\", "//")):
        raise WorkerIsolationError("unsafe_path", "Worker 隔离不接受 UNC 或设备路径")
    try:
        for component in (path, *path.parents):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WorkerIsolationError("linked_path", "隔离路径包含符号链接或 reparse point")
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise WorkerIsolationError("unsafe_path", "无法确认隔离路径的实际身份") from exc
    if directory and not stat.S_ISDIR(info.st_mode):
        raise WorkerIsolationError("unsafe_path", "隔离根必须是目录")
    if not directory and (
        not stat.S_ISREG(info.st_mode) or (info.st_nlink != 1 and not allow_hardlinks)
    ):
        raise WorkerIsolationError("linked_path", "执行程序必须是非硬链接的普通文件")
    if resolved == Path(resolved.anchor):
        raise WorkerIsolationError("broad_root", "磁盘或文件系统根不能作为隔离授权根")
    return resolved


def _reject_task_links(root: Path) -> None:
    """预先存在的硬链接会把 Task 文件写映射到域外，必须在启动前拒绝。"""

    for current, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            item = Path(current) / name
            try:
                info = item.lstat()
            except OSError as exc:
                raise WorkerIsolationError("unsafe_path", "检查 Task 文件身份失败") from exc
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WorkerIsolationError("linked_path", "Task 内含链接或 reparse point")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise WorkerIsolationError("linked_path", "Task 内含硬链接文件")


def validate_spec(
    spec: WorkerIsolationSpec, *, controller_roots: Sequence[Path]
) -> WorkerIsolationSpec:
    """规范化物理根并验证控制面与 Worker 不共享可写祖先。

    将当前已加载的 Python 包源码加入保护域，因此 editable 安装指向 Task 时
    也会拒绝，而不是仅检查调用方声明的 controller_roots。
    """

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", spec.run_id):
        raise WorkerIsolationError("invalid_spec", "run_id 格式无效")
    if type(spec.epoch) is not int or spec.epoch < 1:
        raise WorkerIsolationError("invalid_spec", "lease epoch 必须是正整数")
    if type(spec.allow_network) is not bool or not controller_roots or not spec.protected_roots:
        raise WorkerIsolationError("invalid_spec", "必须声明控制面与保护根及明确网络策略")
    task = _physical_path(spec.task_root)
    protected = tuple(_physical_path(item) for item in spec.protected_roots)
    tools = tuple(_physical_path(item) for item in spec.readonly_tools)
    actual_code = Path(__file__).resolve().parents[1]
    controllers = tuple(_physical_path(item) for item in (*controller_roots, actual_code))
    for other in (*protected, *tools, *controllers):
        if _overlaps(task, other):
            raise WorkerIsolationError("overlapping_roots", "Task 与控制面/保护域/工具目录交叠")
    _reject_task_links(task)
    return WorkerIsolationSpec(task, protected, tools, spec.run_id, spec.epoch, spec.allow_network)


def policy_fingerprint(spec: WorkerIsolationSpec, controller_roots: Sequence[Path]) -> str:
    """指纹仅用于检测已探测策略漂移，不提供签名或身份认证。"""

    payload = {
        "task_root": str(spec.task_root),
        "protected_roots": [str(item) for item in spec.protected_roots],
        "readonly_tools": [str(item) for item in spec.readonly_tools],
        "controller_roots": [str(item) for item in controller_roots],
        "run_id": spec.run_id,
        "epoch": spec.epoch,
        "allow_network": spec.allow_network,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def stage_python_runtime(task_root: Path) -> Path:
    """把当前可信原生 Python 的最小运行时复制到新 Task，绝不改安装目录 ACL。

    供真实无模型探测和 Runtime JSONL fixture 复用。标准库打包为 zip，避免每次
    对上千个测试文件授予/回收 ACL；不包含 site-packages、凭据或用户配置。
    """

    if sys.platform != "win32":
        raise WorkerIsolationError("platform_unavailable", "原生 Python staging 仅支持 Windows")
    task = _physical_path(task_root)
    # 当前解释器安装可能由 uv 通过 junction 暴露；先解析真实只读源，再检查域交叠。
    source = _physical_path(Path(sys.base_prefix).resolve(strict=True))
    if _overlaps(task, source):
        raise WorkerIsolationError("overlapping_roots", "Task 与可信 Python 安装交叠")
    target = task / "python"
    target.mkdir(exist_ok=False)
    version = f"{sys.version_info.major}{sys.version_info.minor}"
    for name in (
        "python.exe",
        f"python{version}.dll",
        "python3.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    ):
        origin = source / name
        if origin.is_file():
            shutil.copy2(origin, target / name)
    libraries = target / "DLLs"
    libraries.mkdir()
    for origin in (source / "DLLs").iterdir():
        if origin.is_file() and origin.suffix.lower() in {".dll", ".pyd"}:
            shutil.copy2(origin, libraries / origin.name)
    excluded = {
        "__pycache__",
        "site-packages",
        "test",
        "tests",
        "idlelib",
        "tkinter",
        "turtledemo",
        "ensurepip",
    }
    with zipfile.ZipFile(
        target / f"python{version}.zip", "w", compression=zipfile.ZIP_STORED
    ) as bundle:
        standard = source / "Lib"
        for current, folders, files in os.walk(standard):
            folders[:] = [name for name in folders if name not in excluded]
            for name in files:
                origin = Path(current) / name
                if origin.suffix == ".py" and not origin.is_symlink():
                    bundle.write(origin, origin.relative_to(standard).as_posix())
    return target / "python.exe"


def worker_environment(spec: WorkerIsolationSpec, *, platform: Mapping[str, str]) -> dict[str, str]:
    """正向构建环境；不继承 Session ID、连接 token、代理、PYTHONPATH 或用户 PATH。"""

    private = spec.task_root / ".ai-dev-os-worker" / f"{spec.run_id}-e{spec.epoch}"
    env = {
        "HOME": str(private / "home"),
        "USERPROFILE": str(private / "home"),
        "TMP": str(private / "tmp"),
        "TEMP": str(private / "tmp"),
        "TMPDIR": str(private / "tmp"),
        "APPDATA": str(private / "home" / "roaming"),
        "LOCALAPPDATA": str(private / "home" / "local"),
        "XDG_CACHE_HOME": str(private / "cache"),
        "XDG_CONFIG_HOME": str(private / "config"),
        "XDG_DATA_HOME": str(private / "data"),
        "CODEX_HOME": str(private / "codex"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    paths = [str(item) for item in spec.readonly_tools]
    for name in ("SystemRoot", "WINDIR"):
        value = platform.get(name)
        if value:
            env[name] = value
    if "SystemRoot" in env:
        system = Path(env["SystemRoot"]) / "System32"
        paths.extend((str(system), str(system / "WindowsPowerShell" / "v1.0")))
        env["COMSPEC"] = str(system / "cmd.exe")
    env["PATH"] = os.pathsep.join(paths)
    return env


@lru_cache(maxsize=1)
def _relevant_lpac_sids() -> frozenset[str]:
    """Only SIDs actually present in our LPAC token can authorize the restricted check."""

    api = _win32()
    c = api.ctypes
    groups, derived = c.POINTER(c.c_void_p)(), c.POINTER(c.c_void_p)()
    group_count, sid_count = api.wt.DWORD(), api.wt.DWORD()
    if not api.kernelbase.DeriveCapabilitySidsFromName(
        "registryRead", c.byref(groups), c.byref(group_count), c.byref(derived), c.byref(sid_count)
    ):
        raise WorkerIsolationError("capability_unavailable", "无法核对 LPAC capability SID")
    try:
        if sid_count.value != 1:
            raise WorkerIsolationError("capability_unavailable", "LPAC capability 数量异常")
        text = c.c_wchar_p()
        if not api.advapi.ConvertSidToStringSidW(derived[0], c.byref(text)):
            raise WorkerIsolationError("capability_unavailable", "无法识别 LPAC capability")
        try:
            # ALL APPLICATION PACKAGES is deliberately absent due to LPAC opt-out.
            return frozenset({"S-1-15-2-2", "S-1-15-3-1", str(text.value)})
        finally:
            api.kernel.LocalFree(text)
    finally:
        for index in range(group_count.value):
            api.kernel.LocalFree(groups[index])
        for index in range(sid_count.value):
            api.kernel.LocalFree(derived[index])
        api.kernel.LocalFree(groups)
        api.kernel.LocalFree(derived)


def _edit_private_sid(path: Path, sid: str, *, grant: bool) -> None:
    """在静止的私有 Task 内修改唯一 SID；既有 ACE 的内容与顺序逐字节保留。"""
    api = _win32()
    c = api.ctypes
    descriptor, dacl, package = c.c_void_p(), c.c_void_p(), c.c_void_p()
    result = api.advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, c.byref(dacl), None, c.byref(descriptor),
    )
    if result:
        raise WorkerIsolationError("acl_update_failed", f"读取 Task DACL 失败: {result}")
    try:
        if not dacl or not api.advapi.ConvertStringSidToSidW(sid, c.byref(package)):
            raise WorkerIsolationError("acl_update_failed", "Task DACL 或临时 SID 无效")
        sid_bytes = c.string_at(package, api.advapi.GetLengthSid(package))
        header = c.string_at(dacl, 8)
        count = struct.unpack_from("<H", header, 4)[0]
        entries: list[bytes] = []
        changed = False
        for index in range(count):
            pointer = c.c_void_p()
            if not api.advapi.GetAce(dacl, index, c.byref(pointer)):
                raise WorkerIsolationError("acl_update_failed", "读取 Task ACE 失败")
            kind, _flags, size = struct.unpack("<BBH", c.string_at(pointer, 4))
            entry = c.string_at(pointer, size)
            if kind == 0 and entry[8:] == sid_bytes:
                changed = True
                continue
            entries.append(entry)
        if grant:
            new = struct.pack("<BBHI", 0, 3 if path.is_dir() else 0,
                              8 + len(sid_bytes), 0x1301BF) + sid_bytes
            # 本次显式 allow 置于继承 ACE 之前，不重排任何原始 allow/deny。
            position = next((i for i, entry in enumerate(entries) if entry[1] & 0x10), len(entries))
            entries.insert(position, new)
            changed = True
        if not changed:
            return
        size = 8 + sum(map(len, entries))
        if size > 65535:
            raise WorkerIsolationError("acl_update_failed", "Task DACL 超出 Win32 长度限制")
        acl = struct.pack("<BBHHH", header[0], header[1], size, len(entries), 0) + b"".join(entries)
        control = struct.unpack_from("<H", c.string_at(descriptor, 4), 2)[0]
        # 自相对描述符只包含 DACL；owner/group/SACL 未请求修改。
        security = struct.pack("<BBHIIII", 1, 0, (control & 0x150C) | 0x8004, 0, 0, 0, 20) + acl
        raw = c.create_string_buffer(security)
        if not api.advapi.SetFileSecurityW(str(path), 4, raw):
            raise WorkerIsolationError("acl_update_failed", f"更新 Task 私有 SID 失败: {c.get_last_error()}")
    finally:
        if package:
            api.kernel.LocalFree(package)
        api.kernel.LocalFree(descriptor)


def _acl_state(path: Path, *, enforce: bool = True) -> str:
    """只读核对实际 DACL；普通 AppContainer/capability 不能从共享 ACE 得到写权限。"""

    api = _win32()
    c = api.ctypes
    descriptor, dacl = c.c_void_p(), c.c_void_p()
    result = api.advapi.GetNamedSecurityInfoW(
        str(path), 1, 7, None, None, c.byref(dacl), None, c.byref(descriptor)
    )
    if result:
        raise WorkerIsolationError("acl_unverifiable", f"无法读取保护对象 DACL: {result}")
    text = c.c_wchar_p()
    try:
        if not dacl:
            raise WorkerIsolationError("unsafe_acl", "保护对象没有限制性 DACL")
        if enforce:
            header = c.string_at(dacl, 8)
            count = struct.unpack_from("<H", header, 4)[0]
            for index in range(count):
                ace = c.c_void_p()
                if not api.advapi.GetAce(dacl, index, c.byref(ace)):
                    raise WorkerIsolationError("acl_unverifiable", "无法读取保护对象 ACE")
                start = c.string_at(ace, 8)
                kind, _flags, _size, mask = struct.unpack("<BBHI", start)
                if kind == 1:  # ACCESS_DENIED_ACE 只能收紧权限。
                    continue
                if kind != 0:
                    raise WorkerIsolationError("acl_unverifiable", "不支持的条件/对象 ACE")
                sid_text = c.c_wchar_p()
                if not api.advapi.ConvertSidToStringSidW(ace.value + 8, c.byref(sid_text)):
                    raise WorkerIsolationError("acl_unverifiable", "无法识别保护对象 SID")
                try:
                    sid = sid_text.value or ""
                finally:
                    api.kernel.LocalFree(sid_text)
                # 只容许 read/execute/read-control/synchronize；禁止写内容、元数据、ACL。
                if sid in _relevant_lpac_sids() and mask & ~0xA01200A9:
                    raise WorkerIsolationError(
                        "unsafe_acl",
                        f"保护对象向 AppContainer 授予写权限: {path}; {sid}; 0x{mask:x}",
                    )
        if not api.advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 7, c.byref(text), None
        ):
            raise WorkerIsolationError("acl_unverifiable", "无法记录保护对象权限指纹")
        return str(text.value)
    finally:
        if text:
            api.kernel.LocalFree(text)
        if descriptor:
            api.kernel.LocalFree(descriptor)


def _protected_acl_fingerprint(spec: WorkerIsolationSpec, controllers: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    roots = {
        *spec.protected_roots,
        *spec.readonly_tools,
        *controllers,
        Path(__file__).resolve().parents[1],
    }
    checked: set[Path] = set()

    def inaccessible(error: OSError) -> None:
        raise WorkerIsolationError("acl_unverifiable", "无法完整枚举保护域") from error

    for root in sorted(roots):
        # DELETE_CHILD on an ancestor can bypass a child's own DELETE denial.
        for ancestor in root.parents:
            if ancestor not in checked:
                checked.add(ancestor)
                digest.update(str(ancestor).encode("utf-8"))
                digest.update(_acl_state(ancestor).encode("utf-8"))
        for current, directories, files in os.walk(root, followlinks=False, onerror=inaccessible):
            for item in (Path(current), *(Path(current) / name for name in (*directories, *files))):
                if item in checked:
                    continue
                checked.add(item)
                if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                    raise WorkerIsolationError("linked_path", "保护目录包含无法核对的链接")
                digest.update(str(item).encode("utf-8"))
                digest.update(_acl_state(item).encode("utf-8"))
    return digest.hexdigest()


class CodexSandboxIsolation:
    """现有 Codex CLI 的边界探测；不能排除自动 setup 时不启动它。

    官方文档和 0.153.4 的公开 CLI 尚未给出 forbid-setup 开关。既有账户或
    setup_marker 不是下一次调用无全局变更的证明，因此在此实现范围内拒绝运行。
    """

    def __init__(self, executable: Path, *, controller_roots: tuple[Path, ...]) -> None:
        self.executable = executable
        self.controller_roots = controller_roots

    def probe(self, spec: WorkerIsolationSpec) -> IsolationCapability:
        try:
            normalized = validate_spec(spec, controller_roots=self.controller_roots)
            binary = _physical_path(self.executable, directory=False)
            if _overlaps(binary, normalized.task_root):
                raise WorkerIsolationError("overlapping_roots", "沙箱程序位于 Worker 可写域")
        except WorkerIsolationError as exc:
            return IsolationCapability(False, "codex-sandbox", str(exc), {"code": exc.code})
        return IsolationCapability(
            False,
            "codex-sandbox",
            "未证明 Codex sandbox 可禁止自动 setup；拒绝潜在的全局账户/ACL/防火墙变更",
            {"code": "setup_mutation_not_excludable", "platform": sys.platform},
        )

    def prepare(self, spec: WorkerIsolationSpec, command: Sequence[str]) -> IsolatedLaunch:
        capability = self.probe(spec)
        raise WorkerIsolationError(
            str(capability.evidence.get("code", "isolation_unavailable")), capability.reason
        )


class AppContainerProcess:
    """创建时已具备 AppContainer token 的挂起进程，由 transport 先入 Job 再恢复。"""

    requires_job_resume = True

    def __init__(
        self, spec: WorkerIsolationSpec, command: Sequence[str], environment: dict[str, str]
    ) -> None:
        self.args = tuple(command)
        self._api = api = _win32()
        self._handle: Any = None
        self.pid = 0
        self.returncode: int | None = None
        self.stdin: io.TextIOWrapper | None = None
        self.stdout: io.TextIOWrapper | None = None
        self.stderr: io.TextIOWrapper | None = None
        self._spec = spec
        self.profile_name = "ai-dev-os-worker-" + uuid.uuid4().hex
        self.profile_directory: Path | None = None
        self.sid = ""
        self._profile_created = False
        self._acl_granted = False
        self._closed = False
        self.cleanup_evidence: dict[str, Any] = {}
        self.isolation_evidence: dict[str, str] = {}
        c = api.ctypes
        package_sid = c.c_void_p()
        network_sid = c.c_void_p()
        registry_sid = c.c_void_p()
        attributes: Any = None
        attributes_initialized = False
        inherited: list[int] = []
        parent_handles: list[int] = []
        try:
            result = api.userenv.CreateAppContainerProfile(
                self.profile_name,
                "AI Dev OS Worker",
                "一次性隔离 Worker",
                None,
                0,
                c.byref(package_sid),
            )
            if result != 0:
                raise WorkerIsolationError(
                    "profile_unavailable", f"创建隔离 profile 失败: {result}"
                )
            self._profile_created = True
            sid_text = c.c_wchar_p()
            if not api.advapi.ConvertSidToStringSidW(package_sid, c.byref(sid_text)):
                raise OSError(c.get_last_error(), "读取 AppContainer SID 失败")
            self.sid = sid_text.value or ""
            api.kernel.LocalFree(sid_text)
            profile_path = c.c_wchar_p()
            result = api.userenv.GetAppContainerFolderPath(self.sid, c.byref(profile_path))
            if result or not profile_path.value:
                raise WorkerIsolationError("profile_unavailable", "无法核对临时 profile 存储位置")
            try:
                self.profile_directory = Path(profile_path.value)
            finally:
                api.ole32.CoTaskMemFree(profile_path)
            for name in (
                "HOME",
                "USERPROFILE",
                "TMP",
                "TEMP",
                "TMPDIR",
                "APPDATA",
                "LOCALAPPDATA",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "CODEX_HOME",
            ):
                Path(environment[name]).mkdir(parents=True, exist_ok=True)
            self._acl_granted = True
            self._change_task_acl(grant=True)
            security = api.SecurityAttributes()
            security.nLength = c.sizeof(security)
            security.bInheritHandle = True
            for index in range(3):
                read, write = api.wt.HANDLE(), api.wt.HANDLE()
                if not api.kernel.CreatePipe(c.byref(read), c.byref(write), c.byref(security), 0):
                    raise OSError(c.get_last_error(), "创建 Worker stdio 失败")
                parent, child = (
                    (write.value, read.value) if index == 0 else (read.value, write.value)
                )
                inherited.append(int(child))
                parent_handles.append(int(parent))
                if not api.kernel.SetHandleInformation(parent, 1, 0):
                    raise OSError(c.get_last_error(), "禁止继承控制面管道失败")
            import msvcrt

            for index, handle in enumerate(tuple(parent_handles)):
                mode = os.O_WRONLY if index == 0 else os.O_RDONLY
                descriptor = msvcrt.open_osfhandle(handle, mode | os.O_BINARY)
                binary_stream = os.fdopen(descriptor, "wb" if index == 0 else "rb", buffering=0)
                stream = io.TextIOWrapper(
                    binary_stream, encoding="utf-8", newline="", write_through=True
                )
                setattr(self, ("stdin", "stdout", "stderr")[index], stream)
                parent_handles.remove(handle)
            size = c.c_size_t()
            api.kernel.InitializeProcThreadAttributeList(None, 3, 0, c.byref(size))
            if not size.value:
                raise OSError(c.get_last_error(), "读取进程属性空间失败")
            attributes = c.create_string_buffer(size.value)
            if not api.kernel.InitializeProcThreadAttributeList(attributes, 3, 0, c.byref(size)):
                raise OSError(c.get_last_error(), "初始化进程属性失败")
            attributes_initialized = True
            capabilities = api.SecurityCapabilities()
            capabilities.AppContainerSid = package_sid
            groups, derived = c.POINTER(c.c_void_p)(), c.POINTER(c.c_void_p)()
            group_count, sid_count = api.wt.DWORD(), api.wt.DWORD()
            if not api.kernelbase.DeriveCapabilitySidsFromName(
                "registryRead",
                c.byref(groups),
                c.byref(group_count),
                c.byref(derived),
                c.byref(sid_count),
            ):
                raise OSError(c.get_last_error(), "创建 LPAC 注册表只读 capability 失败")
            try:
                if sid_count.value != 1:
                    raise WorkerIsolationError("capability_unavailable", "LPAC capability 数量异常")
                registry_sid = c.c_void_p(derived[0])
            finally:
                for index in range(group_count.value):
                    api.kernel.LocalFree(groups[index])
                api.kernel.LocalFree(groups)
                api.kernel.LocalFree(derived)
            capability_items = (api.SidAndAttributes * (2 if spec.allow_network else 1))()
            capability_items[0].Sid = registry_sid
            capability_items[0].Attributes = 4
            capabilities.Capabilities = capability_items
            capabilities.CapabilityCount = len(capability_items)
            if spec.allow_network:
                # 只声明 outbound Internet 能力，不声称具有域名过滤或代理。
                if not api.advapi.ConvertStringSidToSidW("S-1-15-3-1", c.byref(network_sid)):
                    raise OSError(c.get_last_error(), "初始化 Internet capability 失败")
                capability_items[1].Sid = network_sid
                capability_items[1].Attributes = 4
            if not api.kernel.UpdateProcThreadAttribute(
                attributes, 0, 0x20009, c.byref(capabilities), c.sizeof(capabilities), None, None
            ):
                raise OSError(c.get_last_error(), "设置 AppContainer 安全域失败")
            handle_list = (api.wt.HANDLE * 3)(*inherited)
            if not api.kernel.UpdateProcThreadAttribute(
                attributes, 0, 0x20002, c.byref(handle_list), c.sizeof(handle_list), None, None
            ):
                raise OSError(c.get_last_error(), "设置最小继承句柄失败")
            package_policy = api.wt.DWORD(1)  # PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT
            if not api.kernel.UpdateProcThreadAttribute(
                attributes,
                0,
                0x2000F,
                c.byref(package_policy),
                c.sizeof(package_policy),
                None,
                None,
            ):
                raise OSError(c.get_last_error(), "设置 LPAC 最小权限域失败")
            startup = api.StartupInfoEx()
            startup.StartupInfo.cb = c.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            (
                startup.StartupInfo.hStdInput,
                startup.StartupInfo.hStdOutput,
                startup.StartupInfo.hStdError,
            ) = inherited
            startup.lpAttributeList = c.cast(attributes, c.c_void_p)
            process = api.ProcessInformation()
            env_block = c.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0"
            )
            flags = 0x4 | 0x400 | 0x80000 | 0x08000000
            # CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
            # | CREATE_NO_WINDOW；安全 token 在创建时生效，目标代码此时不能执行。
            if not api.kernel.CreateProcessW(
                command[0],
                c.create_unicode_buffer(subprocess.list2cmdline(command)),
                None,
                None,
                True,
                flags,
                env_block,
                str(spec.task_root),
                c.byref(startup),
                c.byref(process),
            ):
                raise OSError(c.get_last_error(), "创建 AppContainer Worker 失败")
            self._handle, self.pid = process.hProcess, process.dwProcessId
            api.kernel.CloseHandle(process.hThread)
            # Profile registration is needed by CreateProcessW, but its package store
            # must not remain an additional writable root when untrusted code executes.
            result = api.userenv.DeleteAppContainerProfile(self.profile_name)
            if result:
                raise WorkerIsolationError(
                    "profile_cleanup_failed", f"启动前删除 profile 失败: {result}"
                )
            self._profile_created = False
            if self.profile_directory.exists():
                raise WorkerIsolationError("profile_cleanup_failed", "临时 profile 存储仍存在")
            self.cleanup_evidence["profile_deleted_before_resume"] = True
        except BaseException:
            self.close()
            raise
        finally:
            for handle in (*inherited, *parent_handles):
                api.kernel.CloseHandle(handle)
            if attributes_initialized:
                api.kernel.DeleteProcThreadAttributeList(attributes)
            if package_sid:
                api.advapi.FreeSid(package_sid)
            if network_sid:
                api.kernel.LocalFree(network_sid)
            if registry_sid:
                api.kernel.LocalFree(registry_sid)

    def _change_task_acl(self, *, grant: bool) -> None:
        root = _physical_path(self._spec.task_root)
        # 不用 icacls 的自动继承重算：CI 的继承-only ACL 会被物化为额外显式 ACE。
        # SetFileSecurityW 有意仅修改单个对象；逐一保留原 ACE 字节和继承标志，
        # 只增删本次 SID，不能以整包旧 ACL 回滚覆盖 Task 后续的权限修改。
        paths = [root]
        for current, directories, files in os.walk(root, followlinks=False):
            paths.extend(Path(current) / name for name in (*directories, *files))
        for path in paths:
            _physical_path(path, directory=path.is_dir())
            _edit_private_sid(path, self.sid, grant=grant)

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if not self._handle:
            return None
        code = self._api.wt.DWORD()
        if not self._api.kernel.GetExitCodeProcess(self._handle, self._api.ctypes.byref(code)):
            raise OSError(self._api.ctypes.get_last_error(), "读取 Worker 状态失败")
        # 259 may also be an intentional exit code; a signaled handle distinguishes it.
        if code.value != 259 or self._api.kernel.WaitForSingleObject(self._handle, 0) == 0:
            self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = 0xFFFFFFFF if timeout is None else min(int(timeout * 1000), 0xFFFFFFFE)
        result = self._api.kernel.WaitForSingleObject(self._handle, milliseconds)
        if result == 258:
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        if result != 0:
            raise OSError(self._api.ctypes.get_last_error(), "等待 Worker 失败")
        return int(self.poll() or 0)

    def kill(self) -> None:
        if (
            self._handle
            and self.poll() is None
            and not self._api.kernel.TerminateProcess(self._handle, 1)
        ):
            raise OSError(self._api.ctypes.get_last_error(), "结束 Worker 失败")

    terminate = kill

    def close(self) -> None:
        """调用前 transport 必须已关闭整个 Job；只回收本次创建的 profile/SID。"""

        if self._closed:
            return
        failures: list[str] = []
        if self._handle:
            self.kill()
            self.wait(timeout=5)
            self._api.kernel.CloseHandle(self._handle)
            self._handle = None
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()
        if self._acl_granted:
            try:
                self._change_task_acl(grant=False)
                self._acl_granted = False
                self.cleanup_evidence["task_sid_removed"] = True
            except (OSError, WorkerIsolationError, subprocess.SubprocessError) as exc:
                failures.append(str(exc))
        if self._profile_created:
            result = self._api.userenv.DeleteAppContainerProfile(self.profile_name)
            if result:
                failures.append(f"删除本次 profile 失败: {result}")
            else:
                self._profile_created = False
                self.cleanup_evidence["profile_deleted_on_failure"] = True
        self._closed = not failures
        if failures:
            raise WorkerIsolationError("cleanup_failed", "; ".join(failures))


_NATIVE_PROBE_SCRIPT = r"""
import ctypes
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys

config = json.loads(sys.stdin.readline())
target = pathlib.Path(config['target'])
results = {}
pathlib.Path('allowed').write_text('allowed', encoding='utf-8')

def denied(name, operation):
    try:
        operation()
    except PermissionError:
        results[name] = True
    else:
        results[name] = False

denied('write', lambda: target.write_text('bad'))
denied('append', lambda: target.open('ab').write(b'bad'))
denied('truncate', lambda: target.open('wb').close())
denied('delete', target.unlink)
denied('rename', lambda: target.rename(target.with_name('renamed')))
denied('mtime', lambda: os.utime(target, (1, 1)))
denied('attributes', lambda: os.chmod(target, stat.S_IREAD))
denied('sibling_write', lambda: target.parent.joinpath('new-authority').write_text('bad'))
denied('hardlink', lambda: os.link(target, pathlib.Path('alias')))
denied('profile_recreate', lambda: pathlib.Path(config['profile_path']).mkdir(parents=True))

kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
kernel.OpenProcess.restype = ctypes.c_void_p
kernel.CloseHandle.argtypes = [ctypes.c_void_p]
handle = kernel.OpenProcess(0x002B, False, config['controller_pid'])
results['control_process'] = not handle and ctypes.get_last_error() == 5
if handle:
    kernel.CloseHandle(handle)
advapi = ctypes.WinDLL('advapi32', use_last_error=True)
advapi.SetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_int,
    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
advapi.SetNamedSecurityInfoW.restype = ctypes.c_ulong
results['acl'] = advapi.SetNamedSecurityInfoW(str(target), 1, 4, None, None, None, None) == 5

child_script = (
    "import pathlib,sys; pathlib.Path('child-allowed').write_text('allowed')\n"
    "try: pathlib.Path(sys.argv[1]).write_text('escaped')\n"
    "except PermissionError: print('CHILD_DENIED')\n"
    "else: raise RuntimeError('escaped')\n"
)
child = subprocess.run([sys.executable, '-S', '-X', 'utf8', '-c', child_script, str(target)],
    capture_output=True, text=True, encoding='utf-8', timeout=10)
results['child_token'] = (child.returncode == 0
    and child.stdout.strip() == 'CHILD_DENIED'
    and pathlib.Path('child-allowed').read_text() == 'allowed')
try:
    escape = subprocess.Popen([sys.executable, '-S', '-c', 'pass'], creationflags=0x01000000)
except PermissionError:
    results['job_breakaway'] = True
else:
    results['job_breakaway'] = False
    escape.wait(timeout=5)
try:
    with socket.socket() as client:
        client.settimeout(2)
        client.connect(('127.0.0.1', config['port']))
except OSError as error:
    results['network'] = getattr(error, 'winerror', None) == 10013
else:
    results['network'] = False
print(json.dumps(results), flush=True)
"""


class WindowsAppContainerIsolation:
    """稳定 Windows AppContainer 后端；仅给 Task 私有目录授权唯一的 run SID。"""

    def __init__(self, *, controller_roots: tuple[Path, ...]) -> None:
        self.controller_roots = controller_roots
        self._proof: dict[str, Any] | None = None
        self._attested: dict[str, str] = {}

    def _launch(
        self,
        spec: WorkerIsolationSpec,
        command: Sequence[str],
        environ: Mapping[str, str] | None = None,
    ) -> AppContainerProcess:
        normalized = validate_spec(spec, controller_roots=self.controller_roots)
        if not command or any(not isinstance(item, str) or "\0" in item for item in command):
            raise WorkerIsolationError("invalid_command", "Worker 命令必须是非空参数数组")
        binary = _physical_path(Path(command[0]), directory=False, allow_hardlinks=True)
        if normalized.task_root not in binary.parents and not any(
            root in binary.parents for root in normalized.readonly_tools
        ):
            raise WorkerIsolationError(
                "untrusted_executable", "执行程序不属于 Task 或显式只读工具根"
            )
        if binary.suffix.lower() != ".exe":
            raise WorkerIsolationError("invalid_command", "Windows Worker 必须指定绝对 EXE 路径")
        env = worker_environment(normalized, platform=os.environ if environ is None else environ)
        return AppContainerProcess(normalized, command, env)

    def probe(self, spec: WorkerIsolationSpec) -> IsolationCapability:
        try:
            normalized = validate_spec(spec, controller_roots=self.controller_roots)
            _win32()
            acl = _protected_acl_fingerprint(normalized, self.controller_roots)
            if self._proof is None:
                self._proof = self._probe_temporary_domain()
            fingerprint = policy_fingerprint(normalized, self.controller_roots)
            self._attested[fingerprint] = acl
            return IsolationCapability(
                True,
                "windows-appcontainer",
                "真实临时域权限负测通过，实际保护根 DACL 已只读核对",
                {
                    **self._proof,
                    "policy_fingerprint": fingerprint,
                    "acl_fingerprint": acl,
                    "network": "internet-capability-unfiltered"
                    if spec.allow_network
                    else "disabled",
                },
            )
        except (OSError, ValueError, WorkerIsolationError, subprocess.SubprocessError) as exc:
            return IsolationCapability(
                False,
                "windows-appcontainer",
                str(exc),
                {"code": getattr(exc, "code", "isolation_unavailable")},
            )

    def launch(
        self,
        spec: WorkerIsolationSpec,
        command: Sequence[str],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> AppContainerProcess:
        normalized = validate_spec(spec, controller_roots=self.controller_roots)
        fingerprint = policy_fingerprint(normalized, self.controller_roots)
        if not self._proof or fingerprint not in self._attested:
            raise WorkerIsolationError("isolation_unverified", "必须先由可信控制面探测此策略")
        # The policy (roots/lease/network) is fixed, not the control plane's file set.
        # Revalidate every current object and ancestor; unsafe ACLs or links still
        # fail closed, while a trusted ledger/event file with a safe ACL is allowed.
        current_acl = _protected_acl_fingerprint(normalized, self.controller_roots)
        process = self._launch(spec, command, environ)
        process.isolation_evidence = {
            "policy_fingerprint": fingerprint,
            "initial_acl_fingerprint": self._attested[fingerprint],
            "launch_acl_fingerprint": current_acl,
        }
        return process

    def _probe_temporary_domain(self) -> dict[str, Any]:
        """不对真实 canonical/用户文件发起攻击；探测对象全部是新建临时 fixture。"""

        # 与产品 transport 使用相同的先入 Job、再恢复流程，不自行用可逃逸 Popen。
        from ..agent_runtime.stdio import _ProcessTree

        with tempfile.TemporaryDirectory(prefix="ai-dev-os-isolation-") as directory:
            root = Path(directory)
            task, protected = root / "task", root / "protected"
            task.mkdir()
            protected.mkdir()
            sentinel = protected / "authority"
            sentinel.write_text("trusted", encoding="utf-8")
            original = sentinel.stat()
            before_acl = _acl_state(sentinel)
            executable = stage_python_runtime(task)
            host = subprocess.Popen(
                [sys.executable, "-S", "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            process: AppContainerProcess | None = None
            tree: Any = None
            try:
                operations = (
                    "write",
                    "append",
                    "truncate",
                    "delete",
                    "rename",
                    "mtime",
                    "attributes",
                    "sibling_write",
                    "hardlink",
                    "control_process",
                    "acl",
                    "child_token",
                    "job_breakaway",
                    "network",
                    "profile_recreate",
                )
                config = {
                    "target": str(sentinel),
                    "controller_pid": host.pid,
                    "port": listener.getsockname()[1],
                }
                spec = WorkerIsolationSpec(task, (protected,), (), "native-probe", 1)
                process = self._launch(
                    spec, [str(executable), "-S", "-X", "utf8", "-c", _NATIVE_PROBE_SCRIPT]
                )
                config["profile_path"] = str(process.profile_directory)
                assert process.stdin is not None
                process.stdin.write(json.dumps(config) + "\n")
                process.stdin.flush()
                if (task / "allowed").exists():
                    raise WorkerIsolationError("isolation_failed", "目标代码在 Job 隔离前执行")
                tree = _ProcessTree(process)
                process.wait(timeout=20)
                tree.kill()
                assert process.stdout is not None and process.stderr is not None
                output, errors = process.stdout.read(), process.stderr.read()
                if process.returncode:
                    raise WorkerIsolationError(
                        "isolation_failed",
                        f"真实隔离探测进程未正常结束: {process.returncode}; {errors[:2000]}",
                    )
                try:
                    results = json.loads(output)
                except ValueError as exc:
                    raise WorkerIsolationError(
                        "isolation_failed", "真实隔离探测没有完整结果"
                    ) from exc
                if results != dict.fromkeys(operations, True):
                    raise WorkerIsolationError(
                        "isolation_failed", f"控制面越权探测未通过: {results}"
                    )
                after = sentinel.stat()
                if (
                    host.poll() is not None
                    or sentinel.read_text() != "trusted"
                    or (task / "allowed").read_text() != "allowed"
                    or _acl_state(sentinel) != before_acl
                    or (after.st_mtime_ns, after.st_mode, after.st_nlink, after.st_ino)
                    != (original.st_mtime_ns, original.st_mode, original.st_nlink, original.st_ino)
                ):
                    raise WorkerIsolationError("isolation_failed", "外部核对发现隔离域越权修改")
            finally:
                if tree is not None:
                    tree.kill()
                if process is not None:
                    process.close()
                listener.close()
                host.terminate()
                host.wait(timeout=5)
                if host.stdin:
                    host.stdin.close()
            return {
                "real_process_probe": True,
                "denied_operations": list(operations),
                "task_write": True,
                "lpac": True,
                "capabilities": ["registryRead"],
                "probe_stderr": errors[:2000],
                "cleanup": dict(process.cleanup_evidence),
            }
