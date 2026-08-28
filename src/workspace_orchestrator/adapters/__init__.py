"""Provider protocols and future concrete adapters."""

from .base import AgentProvider, GitProvider, KnowledgeProvider, TaskProvider
from .git import GitError, LocalGitProvider
from .task import DashiTaskProvider, TaskProviderError

__all__ = [
    "AgentProvider",
    "DashiTaskProvider",
    "GitError",
    "GitProvider",
    "KnowledgeProvider",
    "LocalGitProvider",
    "TaskProvider",
    "TaskProviderError",
]
