"""已发布的顶层导入路径继续可用，原始门禁写入不重新暴露。"""

from __future__ import annotations

import workspace_orchestrator
from workspace_orchestrator import (
    AcceptanceResult,
    GateStore,
    PhaseGateRecord,
    PhaseTransitionGuard,
    ReviewAttestation,
    phase_gate,
)


def test_existing_top_level_gate_imports_remain_compatible() -> None:
    for exported in (
        AcceptanceResult,
        GateStore,
        PhaseGateRecord,
        PhaseTransitionGuard,
        ReviewAttestation,
    ):
        assert exported is getattr(phase_gate, exported.__name__)
        assert exported.__name__ in workspace_orchestrator.__all__


def test_compatible_gate_store_export_does_not_restore_raw_write_methods() -> None:
    for name in ("write", "issue", "write_activation", "write_verification_receipt"):
        assert not hasattr(GateStore, name)
