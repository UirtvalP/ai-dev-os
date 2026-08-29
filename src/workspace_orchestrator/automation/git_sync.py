"""Git root、分支、工作树、提交与变更文件的确定性收集。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from workspace_orchestrator.adapters.base import TaskProvider
from workspace_orchestrator.adapters.git import GitError, LocalGitProvider
from workspace_orchestrator.adapters.task import TaskProviderError


def git_root(project_root: Path, stored_git: dict[str, Any]) -> tuple[Path, str | None]:
    bound_worktree = stored_git.get("worktree")
    if not bound_worktree:
        return project_root, None
    path = Path(str(bound_worktree))
    if not path.is_absolute():
        path = project_root / path
    return path.resolve(), str(bound_worktree)


def collect_git_context(project_root: Path, stored_git: dict[str, Any]) -> dict[str, Any]:
    root, bound_worktree = git_root(project_root, stored_git)
    try:
        provider = LocalGitProvider(root)
        status = provider.status()
        return {
            **status,
            "worktree": bound_worktree or status["worktree"],
            "status": "\n".join(status["changes"]),
            "commits": provider.recent_commits(3),
            "changed_files": provider.changed_files(),
        }
    except GitError as exc:
        return {
            "branch": stored_git.get("branch"),
            "worktree": bound_worktree or str(project_root),
            "status": None,
            "commits": (),
            "changed_files": (),
            "error": str(exc),
        }


def sync_task_git_context(
    task_provider: TaskProvider | None,
    task_ids: Sequence[str],
    tasks: Sequence[object],
    git: dict[str, Any],
) -> None:
    """仅在外部 Task 的 Git 绑定确有差异时写入。"""

    if task_provider is None or not git.get("branch"):
        return
    by_id = {getattr(task, "id", None): task for task in tasks}
    for task_id in dict.fromkeys(task_ids):
        task = by_id.get(task_id)
        if task and (
            getattr(task, "branch", None) == git.get("branch")
            and getattr(task, "worktree", None) == git.get("worktree")
        ):
            continue
        try:
            task_provider.set_git_context(
                task_id, branch=git.get("branch"), worktree=git.get("worktree")
            )
        except TaskProviderError:
            # Git 上下文同步失败时，本地 Snapshot 仍可用。
            continue
