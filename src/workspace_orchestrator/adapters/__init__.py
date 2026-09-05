"""Provider 协议与后续具体适配器。"""

from .agent import CodexAgentProvider, CodexExecProvider, CodexExecutionResult
from .base import AgentProvider, GitProvider, KnowledgeProvider, TaskProvider, TaskProviderError
from .git import GitError, LocalGitProvider
from .package import ToolInstallerError, ToolUpgradeResult, UvToolInstaller
from .task import DashiTaskProvider

__all__ = [
    "AgentProvider",
    "CodexAgentProvider",
    "CodexExecProvider",
    "CodexExecutionResult",
    "DashiTaskProvider",
    "GitError",
    "GitProvider",
    "KnowledgeProvider",
    "LocalGitProvider",
    "TaskProvider",
    "TaskProviderError",
    "ToolInstallerError",
    "ToolUpgradeResult",
    "UvToolInstaller",
]
