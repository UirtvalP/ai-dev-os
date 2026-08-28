"""Restore, checkpoint, and handoff services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .adapters.base import TaskProvider
from .adapters.git import GitError, LocalGitProvider
from .adapters.task import DashiTaskProvider, TaskProviderError
from .workspace import WorkspaceStore, bullets, markdown_sections, now_iso, replace_section


def _summary(value: str, fallback: str = "None") -> str:
    value = value.strip()
    return value if value else fallback


def _git_context(project_root: Path) -> dict[str, Any]:
    try:
        provider = LocalGitProvider(project_root)
        status = provider.status()
        return {
            **status,
            "status": "\n".join(status["changes"]),
            "commits": provider.recent_commits(3),
        }
    except GitError:
        return {"branch": None, "worktree": str(project_root), "status": None, "commits": ()}


def build_snapshot(
    store: WorkspaceStore,
    requirement_id: str,
    task_provider: TaskProvider | None = None,
) -> str:
    data = store.load(requirement_id)
    meta = data["meta"]
    requirement = markdown_sections(data["requirement"])
    state = markdown_sections(data["state"])
    handoff = markdown_sections(data["handoff"])
    verification = markdown_sections(data["verification"])
    git = _git_context(store.project_root)
    task_lines = ["None"]
    provider_name = meta.get("task_provider")
    if task_provider is None and provider_name == "dashi":
        task_provider = DashiTaskProvider(project_id=meta.get("task_project_id") or "local")
    if task_provider is not None:
        try:
            tasks = task_provider.list_tasks(requirement_id)
            task_lines = [f"- {task.id} [{task.status}] {task.title}" for task in tasks] or ["None"]
        except TaskProviderError as exc:
            task_lines = [f"Unavailable ({exc})"]
    stored_git = dict(meta.get("git") or {})
    stored_git.update({key: git[key] for key in ("branch", "worktree") if git.get(key)})
    if stored_git != meta.get("git"):
        meta = store.touch_meta(requirement_id, git=stored_git)

    completed = bullets(state.get("Completed", ""))
    pending = bullets(state.get("Pending", ""))
    decisions_text = data["decisions"].removeprefix("# Decisions").strip()
    verification_lines = []
    for name, body in verification.items():
        status_line = next((line.strip() for line in body.splitlines() if "Status:" in line), body)
        verification_lines.append(f"- {name}: {_summary(status_line)}")

    record_session(store, requirement_id, result="in_progress")
    parts = [
        "# Workspace Context",
        f"Requirement:\n{meta['id']} {meta['title']}",
        f"Goal:\n{_summary(requirement.get('Goal', ''))}",
        f"Status:\n{meta['status']}",
        f"Workflow:\n{meta['workflow']}",
        f"Current Phase:\n{_summary(state.get('Phase', ''))}",
        "Tasks:\n" + "\n".join(task_lines),
        "Completed:\n" + ("\n".join(f"- {item}" for item in completed) or "None"),
        "Pending:\n" + ("\n".join(f"- {item}" for item in pending) or "None"),
        f"Important Decisions:\n{_summary(decisions_text)}",
        (
            "Git:\n"
            f"- branch: {_summary(str(stored_git.get('branch') or ''))}\n"
            f"- worktree: {_summary(str(stored_git.get('worktree') or ''))}\n"
            f"- status: {_summary(str(git.get('status') or ''), 'clean')}"
        ),
        "Verification:\n" + ("\n".join(verification_lines) or "None"),
        f"Last Handoff:\n{_summary(handoff.get('Current State', ''))}",
        f"Next Action:\n{_summary(state.get('Next Action', '') or handoff.get('Next Recommended Action', ''))}",
    ]
    return "\n\n".join(parts).rstrip() + "\n"


def record_session(store: WorkspaceStore, requirement_id: str, *, result: str) -> str | None:
    session_id = os.environ.get("CODEX_THREAD_ID")
    if not session_id:
        return None
    path = store.path_for(requirement_id) / "sessions.json"
    sessions = store.read_json(path)
    timestamp = now_iso()
    existing = next((item for item in sessions if item["id"] == session_id), None)
    if existing:
        existing["ended_at"] = timestamp if result != "in_progress" else existing.get("ended_at")
        existing["result"] = result
    else:
        sessions.append(
            {
                "id": session_id,
                "agent": "codex",
                "started_at": timestamp,
                "ended_at": timestamp if result != "in_progress" else None,
                "task_ids": [],
                "result": result,
            }
        )
    store.write_json(path, sessions)
    return session_id


def checkpoint(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    phase: str | None = None,
    completed: list[str] | None = None,
    next_action: str | None = None,
    verification: str | None = None,
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
    record_session(store, requirement_id, result="in_progress")
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
) -> None:
    checkpoint(store, requirement_id, completed=completed, next_action=next_action)
    data = store.load(requirement_id)
    session_id = record_session(store, requirement_id, result="completed") or "unknown"
    state = markdown_sections(data["state"])
    doc = data["handoff"]
    fields = {
        "Last Session": session_id,
        "Completed": "\n".join(f"- {item}" for item in (completed or [])) or state.get("Completed", "None"),
        "Files Changed": "\n".join(f"- {item}" for item in (files_changed or [])) or "None",
        "Current State": current_state or state.get("In Progress", "None"),
        "Important Context": important_context or "None",
        "Next Recommended Action": next_action or state.get("Next Action", "None"),
        "Known Problems": known_problems or "None",
    }
    for heading, value in fields.items():
        doc = replace_section(doc, heading, value)
    store.write_text(data["path"] / "handoff.md", doc)
    store.touch_meta(requirement_id)
