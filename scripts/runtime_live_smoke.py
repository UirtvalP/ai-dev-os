"""Phase 1 本机受控 smoke；只操作测试创建的临时 Runtime Session。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

from workspace_orchestrator.agent_runtime.contracts import AgentRunRequest
from workspace_orchestrator.composition import create_runtime


def main() -> int:
    # 当前阶段本机已安装 Codex；其他 Runtime 缺失必须是明确的 unavailable。
    # 将来本机安装新 Runtime 后，本 gate 必须增加其 live suite，不能静默跳过。
    with tempfile.TemporaryDirectory(prefix="ai-dev-os-runtime-smoke-") as directory:
        for name in ("cursor", "claude"):
            runtime = create_runtime(name)
            try:
                descriptor = runtime.describe()
                print(json.dumps(asdict(descriptor), ensure_ascii=False))
                if descriptor.available:
                    print(f"{name} 已安装，必须配置并通过其真实 smoke 后再验收本阶段")
                    return 1
                outcome = runtime.start(AgentRunRequest(
                    "unavailable-probe", Path(directory), "不执行任何操作", sandbox="read-only"
                ))
                if outcome.status != "unavailable":
                    print(f"{name} 不可用结果不明确：{outcome.status}")
                    return 1
            finally:
                runtime.close()
    os.environ["AI_DEV_OS_CODEX_LIVE_SMOKE"] = "1"
    return int(pytest.main([
        "-q", "-s", "tests/test_runtime_codex.py::test_live_codex_created_thread_only",
    ]))


if __name__ == "__main__":
    raise SystemExit(main())
