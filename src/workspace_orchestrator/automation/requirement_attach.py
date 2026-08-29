"""项目发现与 Requirement 的确定性选择规则。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


class AutomationAmbiguity(WorkspaceError):
    """只有需要用户选择时才返回的结构化歧义。"""

    status = "ambiguity"


def discover_project_root(start: Path) -> Path:
    """发现共享 Workspace；关联 worktree 回退到 Git 主工作树。"""

    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".workspace").is_dir():
            return candidate
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=resolved,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=resolved,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError:
        return resolved
    if common.returncode == 0:
        common_dir = Path(common.stdout.strip()).resolve()
        main_root = common_dir.parent if common_dir.name == ".git" else None
        if main_root is not None and (main_root / ".workspace").is_dir():
            return main_root
    return Path(top.stdout.strip()).resolve() if top.returncode == 0 else resolved


def select_requirement(
    store: WorkspaceStore,
    session_id: str,
    explicit_requirement_id: str | None = None,
) -> tuple[str, str | None]:
    """按显式 ID、已有绑定、唯一活动 Requirement 的顺序选择。"""

    attached_id = store.attached_requirement_id(session_id)
    if explicit_requirement_id:
        selected_id = explicit_requirement_id.upper()
        store.load(selected_id)
        return selected_id, attached_id
    if attached_id:
        return attached_id, attached_id
    try:
        return store.current_id(), None
    except WorkspaceError as exc:
        if "多个活动" in str(exc):
            raise AutomationAmbiguity(f"ambiguity：{exc}") from exc
        raise
