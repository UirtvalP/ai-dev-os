"""Phase 2 本机强制安全 smoke：跳过、未收集或无真实隔离都不能签发 PASS。"""

from __future__ import annotations

import sys

import pytest

from workspace_orchestrator.console import configure_standard_streams

REQUIRED = (
    "tests/test_worker_isolation.py::test_lpac_staged_python_suspended_and_private",
    "tests/test_worker_isolation.py::test_public_probe_real_attacks_and_lease_fence",
    "tests/test_worker_isolation.py::test_lpac_job_cancellation_reaps_live_descendant",
    "tests/test_runtime_workers.py::test_real_isolated_runtime_can_only_emit_candidate_not_modify_control_plane",
)


class Results:
    def __init__(self) -> None:
        self.passed: set[str] = set()
        self.invalid = False

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped or report.failed:
            self.invalid = True
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)


def main() -> int:
    configure_standard_streams()
    if sys.platform != "win32":
        print("本 Gate 的真实本机隔离要求 Windows LPAC；缺少已实现后端不得用 skip 代替 PASS")
        return 2
    results = Results()
    code = pytest.main(["-q", *REQUIRED], plugins=[results])
    if code or results.invalid or results.passed != set(REQUIRED):
        print("真实隔离证据不完整或失败，拒绝本机 Phase 2 Gate")
        return 1
    print("已确认 Task 写、域外拒绝、取消子树与 Runtime 候选边界；不代表 Requirement 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
