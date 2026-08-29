"""Agent 适配器将产品特定的会话发现逻辑隔离在核心层之外。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass


class AgentProviderError(RuntimeError):
    """Codex 会话操作失败。"""


ArchiveRunner = Callable[[str], None]


def _archive_via_app_server(session_id: str) -> None:
    executable = shutil.which("codex")
    if not executable:
        raise AgentProviderError("未找到 codex CLI，无法归档 Thread")
    messages = (
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "ai_dev_os",
                    "title": "AI Dev OS",
                    "version": "0.1.0",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "thread/archive", "id": 1, "params": {"threadId": session_id}},
    )
    request = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in messages)
    try:
        completed = subprocess.run(
            [executable, "app-server", "--listen", "stdio://"],
            input=request,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentProviderError(f"Codex App Server 不可用：{exc}") from exc
    response = None
    for line in completed.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 1:
            response = message
            break
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "未知错误"
        raise AgentProviderError(f"Codex App Server 归档失败：{detail}")
    if response is None:
        raise AgentProviderError("Codex App Server 未返回 Thread 归档结果")
    if response.get("error"):
        raise AgentProviderError(f"Codex App Server 归档失败：{response['error']}")


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
        """通过 Codex App Server 的公开 thread/archive 契约归档 Thread。"""

        runner = self.archive_runner or _archive_via_app_server
        runner(session_id)
