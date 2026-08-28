"""Agent 适配器将产品特定的会话发现逻辑隔离在核心层之外。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


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
