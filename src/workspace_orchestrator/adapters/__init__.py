"""Provider protocols and future concrete adapters."""

from .base import AgentProvider, GitProvider, KnowledgeProvider, TaskProvider

__all__ = ["AgentProvider", "GitProvider", "KnowledgeProvider", "TaskProvider"]
