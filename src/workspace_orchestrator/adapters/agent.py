"""Agent 适配器将产品特定的会话发现逻辑隔离在核心层之外。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..agent_runtime.codex import codex_command, initialize_codex
from ..agent_runtime.contracts import AgentRunResult
from ..agent_runtime.stdio import JsonRpcStdioClient, RpcResponseError, RpcTransportError

CodexRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]
]


CodexExecutionResult = AgentRunResult


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


class AgentProviderError(RuntimeError):
    """Codex 会话操作失败。"""


ArchiveRunner = Callable[[str], None]


def _archive_via_app_server(session_id: str) -> None:
    try:
        with JsonRpcStdioClient((*codex_command(), "app-server", "--listen", "stdio://")) as client:
            initialize_codex(client, timeout=15)
            client.request("thread/archive", {"threadId": session_id}, timeout=15)
    except (RpcTransportError, RpcResponseError) as exc:
        raise AgentProviderError(f"Codex App Server 不可用：{exc}") from exc


def _exec_summary(stdout: str, stderr: str) -> str:
    """将 V1 exec JSONL 转换为 Provider 无关摘要，不改变原始输出。"""
    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        item = payload.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            messages.append(str(item["text"]))
        elif payload.get("type") == "error" and payload.get("message"):
            messages.append(str(payload["message"]))
        elif payload.get("type") == "turn.failed":
            error = payload.get("error") or {}
            if isinstance(error, dict) and error.get("message"):
                messages.append(str(error["message"]))
    return ((messages[-1] if messages else stderr.strip()) or "Codex 未返回可读结果")[-1800:]


@dataclass(frozen=True, slots=True)
class CodexAgentProvider:
    """发现当前 Codex 线程，同时避免把 Codex 环境变量泄漏到核心层。"""

    environ: Mapping[str, str] | None = None
    archive_runner: ArchiveRunner | None = None

    @property
    def name(self) -> str:
        return "codex"

    def current_session_id(self) -> str | None:
        environ = self.environ if self.environ is not None else os.environ
        return environ.get("CODEX_THREAD_ID") or None

    def archive_session(self, session_id: str) -> None:
        """通过 Codex App Server 的公开契约归档 Thread。"""

        runner = self.archive_runner or _archive_via_app_server
        runner(session_id)


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
            runtime_id="codex-exec",
            summary=_exec_summary(result.stdout, result.stderr),
        )

    def execute(
        self,
        workspace_path: Path,
        prompt: str,
        *,
        sandbox: str = "workspace-write",
        model: str | None = None,
        resume_session_id: str | None = None,
        bypass_hook_trust: bool = False,
    ) -> CodexExecutionResult:
        executable = self.executable or _codex_executable()
        trust_options = ("--dangerously-bypass-hook-trust",) if bypass_hook_trust else ()
        model_options = ("--model", model) if model else ()
        if resume_session_id:
            resumed = self._run(
                (
                    executable,
                    "exec",
                    "resume",
                    "--json",
                    *model_options,
                    *trust_options,
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
                *model_options,
                *trust_options,
                "-c",
                'approval_policy="never"',
                "-C",
                str(workspace_path),
                prompt,
            ),
            workspace_path,
            resumed=False,
        )
