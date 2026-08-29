"""Agent 适配器将产品特定的会话发现逻辑隔离在核心层之外。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CodexRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]
]


@dataclass(frozen=True, slots=True)
class CodexExecutionResult:
    """一次非交互 Codex 执行的结构化结果。"""

    returncode: int
    session_id: str | None
    stdout: str
    stderr: str
    resumed: bool = False


def _default_codex_runner(
    command: Sequence[str],
    cwd: Path,
    environ: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=dict(environ),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=str(exc.stdout or ""),
            stderr=f"Codex 自动执行超时（{timeout:g} 秒）",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def _codex_executable() -> str:
    configured = os.environ.get("AI_DEV_OS_CODEX")
    if configured:
        return configured
    if discovered := shutil.which("codex"):
        return discovered
    return "codex.cmd" if os.name == "nt" else "codex"


def _thread_id(jsonl: str) -> str | None:
    for line in jsonl.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started" and payload.get("thread_id"):
            return str(payload["thread_id"])
    return None


@dataclass(frozen=True, slots=True)
class CodexAgentProvider:
    """发现当前 Codex 线程，同时避免把 Codex 环境变量泄漏到核心层。"""

    environ: Mapping[str, str] | None = None

    @property
    def name(self) -> str:
        return "codex"

    def current_session_id(self) -> str | None:
        environ = self.environ if self.environ is not None else os.environ
        return environ.get("CODEX_THREAD_ID") or None


@dataclass(slots=True)
class CodexExecProvider:
    """通过官方 `codex exec` 边界启动或恢复本地非交互会话。"""

    runner: CodexRunner = _default_codex_runner
    executable: str | None = None
    timeout_seconds: float = 7200

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI"):
            environment.pop(name, None)
        environment["AI_DEV_OS_DISPATCHER_CHILD"] = "1"
        return environment

    def _run(self, command: Sequence[str], cwd: Path, *, resumed: bool) -> CodexExecutionResult:
        result = self.runner(command, cwd, self._environment(), self.timeout_seconds)
        return CodexExecutionResult(
            returncode=result.returncode,
            session_id=_thread_id(result.stdout),
            stdout=result.stdout,
            stderr=result.stderr,
            resumed=resumed,
        )

    def execute(
        self,
        workspace_path: Path,
        prompt: str,
        *,
        sandbox: str = "workspace-write",
        resume_session_id: str | None = None,
    ) -> CodexExecutionResult:
        executable = self.executable or _codex_executable()
        if resume_session_id:
            resumed = self._run(
                (
                    executable,
                    "exec",
                    "resume",
                    "--json",
                    "--dangerously-bypass-hook-trust",
                    resume_session_id,
                    prompt,
                ),
                workspace_path,
                resumed=True,
            )
            if resumed.returncode == 0:
                return resumed
        return self._run(
            (
                executable,
                "exec",
                "--json",
                "--sandbox",
                sandbox,
                "--dangerously-bypass-hook-trust",
                "-c",
                'approval_policy="never"',
                "-C",
                str(workspace_path),
                prompt,
            ),
            workspace_path,
            resumed=False,
        )
