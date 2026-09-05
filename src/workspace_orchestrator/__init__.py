"""面向 AI 编码 Agent 的持久化需求工作区。"""

from .models import Requirement, Session, Task, WorkflowComplexity, Workspace
from .phase_gate import (
    AcceptanceResult,
    GateStore,
    PhaseGateRecord,
    PhaseTransitionGuard,
    ReviewAttestation,
)
from .workspace import WorkspaceStore

__all__ = [
    "AcceptanceResult",
    "GateStore",
    "PhaseGateRecord",
    "PhaseTransitionGuard",
    "Requirement",
    "ReviewAttestation",
    "Session",
    "Task",
    "WorkflowComplexity",
    "Workspace",
    "WorkspaceStore",
]
__version__ = "0.2.0"
