"""恢复、检查点与交接服务。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .adapters.base import AgentProvider, TaskProvider
from .adapters.git import GitError, LocalGitProvider
from .adapters.task import DashiTaskProvider, TaskProviderError
from .intent import requirement_intent_summary, summarize_document
from .workspace import (
    SECTION_LABELS,
    WorkspaceStore,
    bullets,
    markdown_sections,
    now_iso,
    replace_section,
)


def _summary(value: str, fallback: str = "无") -> str:
    value = value.strip()
    return value if value else fallback


def _display_state(value: str) -> str:
    labels = {
        "draft": "草稿",
        "ready": "就绪",
        "in_progress": "进行中",
        "in_review": "审查中",
        "done": "已完成",
        "todo": "待处理",
        "blocked": "已阻塞",
        "tiny": "微型",
        "normal": "常规",
        "complex": "复杂",
        "research": "研究",
        "implementation": "实现",
    }
    label = labels.get(value)
    return f"{value}（{label}）" if label else value


def _git_root(project_root: Path, stored_git: dict[str, Any]) -> tuple[Path, str | None]:
    bound_worktree = stored_git.get("worktree")
    if not bound_worktree:
        return project_root, None
    path = Path(str(bound_worktree))
    if not path.is_absolute():
        path = project_root / path
    return path.resolve(), str(bound_worktree)


def _git_context(project_root: Path, stored_git: dict[str, Any]) -> dict[str, Any]:
    git_root, bound_worktree = _git_root(project_root, stored_git)
    try:
        provider = LocalGitProvider(git_root)
        status = provider.status()
        return {
            **status,
            "worktree": bound_worktree or status["worktree"],
            "status": "\n".join(status["changes"]),
            "commits": provider.recent_commits(3),
        }
    except GitError as exc:
        return {
            "branch": stored_git.get("branch"),
            "worktree": bound_worktree or str(project_root),
            "status": None,
            "commits": (),
            "error": str(exc),
        }


def _configured_task_provider(meta: dict[str, Any]) -> TaskProvider | None:
    if meta.get("task_provider") == "dashi":
        return DashiTaskProvider(project_id=meta.get("task_project_id") or "local")
    return None


def build_snapshot(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None = None,
    agent_provider: AgentProvider | None = None,
    task_ids: Sequence[str] = (),
) -> str:
    data = store.load(requirement_id)
    meta = data["meta"]
    requirement = markdown_sections(data["requirement"])
    user_principles = summarize_document(store.project_root / "USER_PRINCIPLES.md")
    project_intent = summarize_document(
        store.project_root / "PROJECT_INTENT.md",
        headings=("Purpose", "Desired Outcome", "Must Not Become", "Trade-off Priorities"),
    )
    requirement_intent = requirement_intent_summary(data["intent"])
    state = markdown_sections(data["state"])
    handoff = markdown_sections(data["handoff"])
    verification = markdown_sections(data["verification"])
    stored_git = dict(meta.get("git") or {})
    git = _git_context(store.project_root, stored_git)
    task_lines = ["无"]
    tasks = ()
    if task_provider is None:
        task_provider = _configured_task_provider(meta)
    if task_provider is not None:
        try:
            tasks = tuple(task_provider.list_tasks(requirement_id))
            task_lines = [
                f"- {task.id} [{_display_state(task.status)}] {task.title}" for task in tasks
            ] or ["无"]
        except TaskProviderError as exc:
            task_lines = [f"不可用（{exc}）"]
    stored_git.update({key: git[key] for key in ("branch", "worktree") if git.get(key)})
    if stored_git != meta.get("git"):
        meta = store.touch_meta(requirement_id, git=stored_git)

    completed = bullets(state.get("Completed", ""))
    pending = bullets(state.get("Pending", ""))
    decisions_text = (
        data["decisions"].removeprefix("# Decisions").removeprefix("# 决策").strip()
    )
    verification_lines = []
    for name, body in verification.items():
        status_line = next(
            (
                line.strip()
                for line in body.splitlines()
                if "Status:" in line or "状态：" in line or "状态:" in line
            ),
            body,
        )
        verification_lines.append(f"- {SECTION_LABELS.get(name, name)}：{_summary(status_line)}")

    git_status = git.get("status")
    if git.get("error"):
        git_status = f"不可用（{git['error']}）"

    session_id = agent_provider.current_session_id() if agent_provider else None
    associated_task_ids = list(task_ids)
    if session_id:
        associated_task_ids.extend(
            task.id
            for task in tasks
            if task.status == "in_progress" or session_id in task.session_ids
        )
    record_session(
        store,
        requirement_id,
        result="in_progress",
        agent_provider=agent_provider,
        task_provider=task_provider,
        task_ids=associated_task_ids,
    )
    parts = [
        "# 工作区上下文",
        f"需求：\n{meta['id']} {meta['title']}",
        f"目标：\n{_summary(requirement.get('Goal', ''))}",
        "用户原则：\n" + "\n".join(f"- {item}" for item in user_principles),
        "项目意图：\n" + "\n".join(f"- {item}" for item in project_intent),
        "需求意图：\n" + "\n".join(f"- {item}" for item in requirement_intent),
        f"状态：\n{_display_state(meta['status'])}",
        f"工作流：\n{_display_state(meta['workflow'])}",
        f"当前阶段：\n{_display_state(_summary(state.get('Phase', '')))}",
        "任务：\n" + "\n".join(task_lines),
        "已完成：\n" + ("\n".join(f"- {item}" for item in completed) or "无"),
        "待处理：\n" + ("\n".join(f"- {item}" for item in pending) or "无"),
        f"重要决策：\n{_summary(decisions_text)}",
        (
            "Git：\n"
            f"- 分支：{_summary(str(stored_git.get('branch') or ''))}\n"
            f"- 工作树：{_summary(str(stored_git.get('worktree') or ''))}\n"
            f"- 状态：{_summary(str(git_status or ''), '干净')}"
        ),
        "验证：\n" + ("\n".join(verification_lines) or "无"),
        f"上次交接：\n{_summary(handoff.get('Current State', ''))}",
        f"下一步行动：\n{_summary(state.get('Next Action', '') or handoff.get('Next Recommended Action', ''))}",
    ]
    return "\n\n".join(parts).rstrip() + "\n"


def record_session(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    result: str,
    agent_provider: AgentProvider | None = None,
    task_provider: TaskProvider | None = None,
    task_ids: Sequence[str] = (),
) -> str | None:
    session_id = agent_provider.current_session_id() if agent_provider else None
    if not session_id:
        return None
    path = store.path_for(requirement_id) / "sessions.json"
    sessions = store.read_json(path)
    timestamp = now_iso()
    existing = next((item for item in sessions if item["id"] == session_id), None)
    normalized_task_ids = list(dict.fromkeys(task_id for task_id in task_ids if task_id))
    if existing:
        existing["ended_at"] = timestamp if result != "in_progress" else existing.get("ended_at")
        existing["result"] = result
        existing["task_ids"] = list(
            dict.fromkeys([*existing.get("task_ids", []), *normalized_task_ids])
        )
    else:
        sessions.append(
            {
                "id": session_id,
                "agent": agent_provider.name,
                "started_at": timestamp,
                "ended_at": timestamp if result != "in_progress" else None,
                "task_ids": normalized_task_ids,
                "result": result,
            }
        )
    store.write_json(path, sessions)
    if task_provider is not None:
        for task_id in normalized_task_ids:
            try:
                task_provider.link_session(task_id, session_id)
            except TaskProviderError:
                # 外部任务板离线时，工作区状态仍应保持可用。
                continue
    return session_id


def checkpoint(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    phase: str | None = None,
    completed: list[str] | None = None,
    next_action: str | None = None,
    verification: str | None = None,
    agent_provider: AgentProvider | None = None,
    task_provider: TaskProvider | None = None,
    task_ids: Sequence[str] = (),
) -> None:
    data = store.load(requirement_id)
    state = data["state"]
    if phase:
        state = replace_section(state, "Phase", phase)
    if completed:
        current = bullets(markdown_sections(state).get("Completed", ""))
        merged = list(dict.fromkeys([*current, *completed]))
        state = replace_section(state, "Completed", "\n".join(f"- {item}" for item in merged))
    if next_action:
        state = replace_section(state, "Next Action", next_action)
    store.write_text(data["path"] / "state.md", state)
    if completed:
        plan = data["plan"]
        for item in completed:
            unchecked = f"- [ ] {item}"
            in_progress = f"- [-] {item}"
            if unchecked in plan:
                plan = plan.replace(unchecked, f"- [x] {item}", 1)
            elif in_progress in plan:
                plan = plan.replace(in_progress, f"- [x] {item}", 1)
            elif f"- [x] {item}" not in plan:
                plan = plan.rstrip() + f"\n- [x] {item}\n"
        store.write_text(data["path"] / "plan.md", plan)
    if verification:
        verification_doc = replace_section(data["verification"], "Latest Check", verification)
        store.write_text(data["path"] / "verification.md", verification_doc)
    if task_provider is None:
        task_provider = _configured_task_provider(data["meta"])
    record_session(
        store,
        requirement_id,
        result="in_progress",
        agent_provider=agent_provider,
        task_provider=task_provider,
        task_ids=task_ids,
    )
    status = data["meta"]["status"]
    if status in {"draft", "ready"} and phase and phase not in {"draft", "ready"}:
        status = "in_progress"
    store.touch_meta(requirement_id, status=status)


def handoff(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    completed: list[str] | None = None,
    files_changed: list[str] | None = None,
    current_state: str | None = None,
    important_context: str | None = None,
    next_action: str | None = None,
    known_problems: str | None = None,
    agent_provider: AgentProvider | None = None,
    task_provider: TaskProvider | None = None,
    task_ids: Sequence[str] = (),
) -> None:
    checkpoint(
        store,
        requirement_id,
        completed=completed,
        next_action=next_action,
        agent_provider=agent_provider,
        task_provider=task_provider,
        task_ids=task_ids,
    )
    data = store.load(requirement_id)
    if task_provider is None:
        task_provider = _configured_task_provider(data["meta"])
    session_id = record_session(
        store,
        requirement_id,
        result="completed",
        agent_provider=agent_provider,
        task_provider=task_provider,
        task_ids=task_ids,
    ) or "未知"
    git_root, _ = _git_root(store.project_root, dict(data["meta"].get("git") or {}))
    try:
        git_files = LocalGitProvider(git_root).changed_files()
    except GitError:
        git_files = ()
    changed_files = list(
        dict.fromkeys(
            path
            for path in [*git_files, *(files_changed or [])]
            if path != ".workspace" and not path.startswith(".workspace/")
        )
    )
    state = markdown_sections(data["state"])
    doc = data["handoff"]
    fields = {
        "Last Session": session_id,
        "Completed": "\n".join(f"- {item}" for item in (completed or [])) or state.get("Completed", "无"),
        "Files Changed": "\n".join(f"- {item}" for item in changed_files) or "无",
        "Current State": current_state or state.get("In Progress", "无"),
        "Important Context": important_context or "无",
        "Next Recommended Action": next_action or state.get("Next Action", "无"),
        "Known Problems": known_problems or "无",
    }
    for heading, value in fields.items():
        doc = replace_section(doc, heading, value)
    store.write_text(data["path"] / "handoff.md", doc)
    store.touch_meta(requirement_id)
