"""Session 发现、注册、复用与结束。"""

from __future__ import annotations

from collections.abc import Sequence

from workspace_orchestrator.adapters.base import AgentProvider, TaskProvider
from workspace_orchestrator.adapters.task import TaskProviderError
from workspace_orchestrator.workspace import WorkspaceStore, now_iso


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
) -> None:
    """幂等注册 Session，并只建立尚不存在的外部 Task 绑定。"""

    path = store.path_for(requirement_id) / "sessions.json"
    sessions = store.read_json(path)
    timestamp = now_iso()
    existing = next((item for item in sessions if item["id"] == session_id), None)
    normalized = list(dict.fromkeys(task_id for task_id in task_ids if task_id))
    newly_linked = normalized
    if existing:
        previous = existing.get("task_ids", [])
        if existing.get("result") == "in_progress":
            newly_linked = [task_id for task_id in normalized if task_id not in previous]
        existing["ended_at"] = None
        existing["result"] = "in_progress"
        existing["task_ids"] = list(dict.fromkeys([*previous, *normalized]))
    else:
        sessions.append(
            {
                "id": session_id,
                "agent": agent_name,
                "started_at": timestamp,
                "ended_at": None,
                "task_ids": normalized,
                "result": "in_progress",
            }
        )
    store.write_json(path, sessions)
    if task_provider is not None:
        for task_id in newly_linked:
            try:
                task_provider.link_session(task_id, session_id)
            except TaskProviderError:
                # 外部任务板离线不能破坏本地可恢复状态。
                continue


def end_session(
    store: WorkspaceStore,
    requirement_id: str,
    session_id: str,
    *,
    result: str = "detached",
    task_provider: TaskProvider | None = None,
) -> None:
    """幂等结束 Session，同时清除 dashi 当前 Thread 绑定。"""

    path = store.path_for(requirement_id) / "sessions.json"
    sessions = store.read_json(path)
    existing = next(
        (
            item
            for item in sessions
            if item.get("id") == session_id and item.get("result") == "in_progress"
        ),
        None,
    )
    if existing is None:
        return
    if task_provider is not None:
        for task_id in existing.get("task_ids", []):
            try:
                task_provider.unlink_session(task_id, session_id)
            except TaskProviderError:
                continue
    existing["ended_at"] = now_iso()
    existing["result"] = result
    store.write_json(path, sessions)
