"""包管理 Adapter：隔离产品 CLI 与全局工具安装器。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ToolRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ToolScheduler = Callable[[str, str], Path]


class ToolInstallerError(RuntimeError):
    """全局 CLI 更新失败。"""


@dataclass(frozen=True, slots=True)
class ToolUpgradeResult:
    """一次全局 CLI 更新的结构化结果。"""

    source: str
    details: str
    scheduled: bool = False
    result_path: Path | None = None


def _run_tool(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def _schedule_windows_upgrade(executable: str, source: str) -> Path:
    """当前 CLI 退出后再更新，避免 Windows 锁住自身 tool 环境。"""

    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise ToolInstallerError("未找到 PowerShell，无法安排全局 CLI 更新")
    identifier = uuid.uuid4().hex
    temporary_root = Path(tempfile.gettempdir())
    script_path = temporary_root / f"ai-dev-os-upgrade-{identifier}.ps1"
    result_path = temporary_root / f"ai-dev-os-upgrade-{identifier}.log"
    script_path.write_text(
        """param(
    [string]$UvPath,
    [string]$Source,
    [string]$ResultPath
)
Start-Sleep -Milliseconds 1500
$output = & $UvPath tool install --force --refresh -- $Source 2>&1 | Out-String
$code = $LASTEXITCODE
[IO.File]::WriteAllText(
    $ResultPath,
    ("exit_code={0}`n{1}" -f $code, $output),
    [Text.UTF8Encoding]::new($false)
)
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
exit $code
""",
        encoding="utf-8",
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script_path),
            "-UvPath",
            executable,
            "-Source",
            source,
            "-ResultPath",
            str(result_path),
        ],
        close_fds=True,
        creationflags=creation_flags,
    )
    return result_path


class UvToolInstaller:
    """通过 uv 重新安装官方来源，使 CLI 能力全局更新。"""

    def __init__(
        self,
        *,
        executable: str | None = None,
        runner: ToolRunner = _run_tool,
        scheduler: ToolScheduler = _schedule_windows_upgrade,
        platform: str = os.name,
    ) -> None:
        self._executable = (
            executable
            or os.environ.get("AI_DEV_OS_UV")
            or shutil.which("uv")
            or ""
        )
        self._runner = runner
        self._scheduler = scheduler
        self._platform = platform

    def upgrade(self, source: str) -> ToolUpgradeResult:
        if not self._executable:
            raise ToolInstallerError("未找到 uv，无法更新全局 AI Dev OS CLI")
        if self._platform == "nt":
            result_path = self._scheduler(self._executable, source)
            return ToolUpgradeResult(
                source=source,
                details="CLI 退出后将由独立更新进程完成安装。",
                scheduled=True,
                result_path=result_path,
            )
        result = self._runner(
            [
                self._executable,
                "tool",
                "install",
                "--force",
                "--refresh",
                "--",
                source,
            ]
        )
        details = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        if result.returncode != 0:
            suffix = f"：{details}" if details else ""
            raise ToolInstallerError(f"全局 AI Dev OS CLI 更新失败{suffix}")
        return ToolUpgradeResult(source=source, details=details)
