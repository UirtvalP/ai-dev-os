"""测试隔离：默认 dashi 路径必须降级，不能写入用户的真实任务面板。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_external_task_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHI_TASKCTL", "ai-dev-os-test-taskctl-unavailable")
    monkeypatch.setenv("AI_DEV_OS_DISABLE_AUTOSTART", "1")
