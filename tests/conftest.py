"""测试隔离：默认 dashi 路径必须降级，不能写入用户的真实任务面板。"""

from __future__ import annotations

from pathlib import Path

import pytest

from workspace_orchestrator import user_config


@pytest.fixture(autouse=True)
def isolate_external_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DASHI_TASKCTL", "ai-dev-os-test-taskctl-unavailable")
    monkeypatch.setenv("AI_DEV_OS_DISABLE_AUTOSTART", "1")
    monkeypatch.setattr(user_config, "user_config_root", lambda: tmp_path / "user-config")
