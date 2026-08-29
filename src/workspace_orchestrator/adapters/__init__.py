"""Provider 协议与后续具体适配器。"""

from .agent import CodexAgentProvider, CodexExecProvider, CodexExecutionResult
from .base import AgentProvider, GitProvider, KnowledgeProvider, TaskProvider
from .git import GitError, LocalGitProvider
from .task import DashiTaskProvider, TaskProviderError

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
]
