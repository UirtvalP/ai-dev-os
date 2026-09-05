from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("workspace_orchestrator.product_cli", "管理全局 AI Dev OS CLI 与项目接入"),
        ("workspace_orchestrator.cli", "管理面向 AI 编码 Agent 的持久化需求工作区"),
    ],
)
def test_cli_help_is_utf8_when_parent_requests_cp1252(module: str, expected: str) -> None:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        env=environment,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert expected in result.stdout.decode("utf-8")
