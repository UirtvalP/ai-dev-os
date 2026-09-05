"""Session 发现、注册、复用与结束。"""

from __future__ import annotations

from collections.abc import Sequence

from workspace_orchestrator.adapters.base import AgentProvider, TaskProvider, TaskProviderError
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore, now_iso


def require_session_id(agent_provider: AgentProvider) -> str:
    """确定性读取当前 Session ID；缺失时立即失败。"""

    session_id = agent_provider.current_session_id()
    if not session_id:
        from workspace_orchestrator.workspace import WorkspaceError

        raise WorkspaceError("未检测到 CODEX_THREAD_ID，无法自动接入当前 Codex Thread")
    return session_id


def session_task_ids(
    store: WorkspaceStore, requirement_id: str, session_id: str
) -> tuple[str, ...]:
    """返回当前活动绑定中的 Task，供重复 bootstrap 直接复用。"""

    data = store.load(requirement_id)
    session = next(
        (
            item
            for item in data["sessions"]
            if item.get("id") == session_id and item.get("result") == "in_progress"
        ),
        None,
    )
    return tuple(session.get("task_ids", ())) if session else ()


def attach_session(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    session_id: str,
    agent_name: str,
    task_provider: TaskProvider | None = None,
    task_ids: Sequence[str] = (),
    head_commit: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
) -> None:
    """幂等注册 Session，并只建立尚不存在的外部 Task 绑定。"""

    path = store.path_for(requirement_id) / "sessions.json"
    # Session 是可替换的执行上下文，但一个活跃 Session 只能服务一个 Requirement。
    # 使用 Workspace 总锁把“检查其他绑定 + 写入当前绑定”合并为一次原子操作。
    with store.locked():
        conflicts = store.active_session_conflicts()
        if session_id in conflicts:
            linked = ", ".join(conflicts[session_id])
            raise WorkspaceError(
                f"会话 {session_id} 同时关联了多个需求：{linked}；"
                "请先运行 workspace doctor 查看并显式处理遗留绑定"
            )
        existing_requirement = store.requirement_id_for_session(session_id)
        if existing_requirement and existing_requirement != requirement_id.upper():
            raise WorkspaceError(
                f"会话 {session_id} 已活跃绑定到 {existing_requirement}，"
                f"不能同时接入 {requirement_id.upper()}；请先交接或结束旧会话"
            )
        sessions = store.read_json(path)
        timestamp = now_iso()
        existing = next((item for item in sessions if item["id"] == session_id), None)
        normalized = list(dict.fromkeys(task_id for task_id in task_ids if task_id))
        # 同一 Requirement 可有多个 Agent，但只能处理不同 Task，且并行写入必须隔离。
        # 这里是确定性门禁，不负责创建或分配 worktree。
        for active in sessions:
            if active.get("id") == session_id or active.get("result") != "in_progress":
                continue
            active_tasks = set(active.get("task_ids", ()))
            if not active_tasks or not normalized:
                continue
            overlap = active_tasks.intersection(normalized)
            if overlap:
                raise WorkspaceError(
                    f"Task {', '.join(sorted(overlap))} 已由活跃 Session {active['id']} 处理"
                )
            active_worktree = str(active.get("worktree") or "")
            active_branch = str(active.get("branch") or "")
            if not worktree or not branch or not active_worktree or not active_branch:
                raise WorkspaceError(
                    "同一 Requirement 的并行 Agent 必须为不同 Task 配置独立 branch 与 worktree"
                )
            if worktree == active_worktree or branch == active_branch:
                raise WorkspaceError("同一 Requirement 的并行 Agent 不能共享 branch 或 worktree")
        if existing:
            previous = existing.get("task_ids", [])
            was_active = existing.get("result") == "in_progress"
            pending_before = existing.get("pending_link_task_ids", [])
            existing["ended_at"] = None
            existing["result"] = "in_progress"
            existing["task_ids"] = list(dict.fromkeys([*previous, *normalized]))
            if head_commit and not existing.get("started_head"):
                existing["started_head"] = head_commit
            if branch:
                existing["branch"] = branch
            if worktree:
                existing["worktree"] = worktree
            links_to_attempt = list(
                dict.fromkeys(
                    [
                        *pending_before,
                        *(task_id for task_id in normalized if task_id not in previous),
                        *(() if was_active else normalized),
                    ]
                )
            )
        else:
            existing = {
                "id": session_id,
                "agent": agent_name,
                "started_at": timestamp,
                "ended_at": None,
                "task_ids": normalized,
                "started_head": head_commit,
                "branch": branch,
                "worktree": worktree,
                "result": "in_progress",
            }
            sessions.append(existing)
            links_to_attempt = normalized
        pending_unlinks = tuple(
            (str(item["id"]), task_id)
            for item in sessions
            for task_id in item.get("pending_unlink_task_ids", ())
        )
        store.write_json(path, sessions)
    if task_provider is None:
        return

    # Provider I/O 位于本地状态锁之外；Hook 被强杀时不会遗留工作区锁。
    failed_links: list[str] = []
    for task_id in links_to_attempt:
        try:
            task_provider.link_session(task_id, session_id)
        except TaskProviderError:
            failed_links.append(task_id)
    failed_unlinks: set[tuple[str, str]] = set()
    for pending_session_id, task_id in pending_unlinks:
        try:
            task_provider.unlink_session(task_id, pending_session_id)
        except TaskProviderError:
            failed_unlinks.add((pending_session_id, task_id))

    with store.locked(requirement_id):
        sessions = store.read_json(path)
        existing = next((item for item in sessions if item["id"] == session_id), None)
        if existing is not None and existing.get("result") == "in_progress":
            current_links = set(existing.get("pending_link_task_ids", ()))
            current_links.difference_update(links_to_attempt)
            current_links.update(failed_links)
            if current_links:
                existing["pending_link_task_ids"] = sorted(current_links)
            else:
                existing.pop("pending_link_task_ids", None)
        attempted = set(pending_unlinks)
        for item in sessions:
            current_unlinks = set(item.get("pending_unlink_task_ids", ()))
            current_unlinks.difference_update(
                task_id
                for pending_session_id, task_id in attempted
                if pending_session_id == item["id"]
            )
            current_unlinks.update(
                task_id
                for pending_session_id, task_id in failed_unlinks
                if pending_session_id == item["id"]
            )
            if current_unlinks:
                item["pending_unlink_task_ids"] = sorted(current_unlinks)
            else:
                item.pop("pending_unlink_task_ids", None)
        store.write_json(path, sessions)


def end_session(
    store: WorkspaceStore,
    requirement_id: str,
    session_id: str,
    *,
    result: str = "detached",
    task_provider: TaskProvider | None = None,
    allowed_results: Sequence[str] = ("in_progress",),
) -> None:
    """幂等结束 Session，同时清除 dashi 当前 Thread 绑定。"""

    path = store.path_for(requirement_id) / "sessions.json"
    with store.locked(requirement_id):
        sessions = store.read_json(path)
        existing = next(
            (
                item
                for item in sessions
                if item.get("id") == session_id and item.get("result") in allowed_results
            ),
            None,
        )
        if existing is None:
            return
        task_ids = list(dict.fromkeys(existing.get("task_ids", ())))
        existing["ended_at"] = now_iso()
        existing["result"] = result
        existing.pop("pending_link_task_ids", None)
        existing["pending_unlink_task_ids"] = task_ids
        store.write_json(path, sessions)
    if task_provider is None:
        return
    remaining: list[str] = []
    for task_id in task_ids:
        try:
            task_provider.unlink_session(task_id, session_id)
        except TaskProviderError:
            remaining.append(task_id)
    with store.locked(requirement_id):
        sessions = store.read_json(path)
        existing = next((item for item in sessions if item.get("id") == session_id), None)
        if existing is None or existing.get("result") in allowed_results:
            return
        current = set(existing.get("pending_unlink_task_ids", ()))
        current.difference_update(task_ids)
        current.update(remaining)
        if current:
            existing["pending_unlink_task_ids"] = sorted(current)
        else:
            existing.pop("pending_unlink_task_ids", None)
        store.write_json(path, sessions)
