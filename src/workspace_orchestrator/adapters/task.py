"""Optional dashi-taskboard adapter using its documented JSON CLI boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace_orchestrator.models import Task

CommandRunner = Callable[[Sequence[str]], str]


class TaskProviderError(RuntimeError):
    pass


def _default_runner(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            command, text=True, encoding="utf-8", capture_output=True, check=False
        )
    except OSError as exc:
        raise TaskProviderError(f"taskctl is unavailable: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown taskctl error"
        raise TaskProviderError(message)
    return result.stdout


def _relation_ids(values: Sequence[object]) -> tuple[str, ...]:
    return tuple(
        str(value.get("identifier") or value.get("id")) if isinstance(value, dict) else str(value)
        for value in values
    )


def _task(data: dict[str, Any]) -> Task:
    development = data.get("developmentContext") or {}
    relations = data.get("relations") or {}
    refs = data.get("conversationRefs") or []
    session_ids = tuple(dict.fromkeys(str(ref["threadId"]) for ref in refs if ref.get("threadId")))
    parent = relations.get("parent")
    parent_id = None
    if parent:
        parent_id = str(parent.get("identifier") or parent.get("id")) if isinstance(parent, dict) else str(parent)
    return Task(
        id=str(data.get("identifier") or data["id"]),
        raw_id=str(data["id"]),
        project_id=data.get("projectId"),
        title=str(data["title"]),
        description=str(data.get("description", "")),
        status=str(data.get("status", "todo")),
        priority=data.get("priority"),
        parent_id=parent_id,
        blocked_by=_relation_ids(relations.get("blockedBy", ())),
        blocks=_relation_ids(relations.get("blocks", ())),
        branch=development.get("branch"),
        worktree=development.get("path") if development.get("type") == "worktree" else None,
        session_ids=session_ids,
        labels=tuple(data.get("labels", ())),
        version=data.get("version"),
    )


def _default_executable() -> str:
    configured = os.environ.get("DASHI_TASKCTL")
    if configured:
        return configured
    discovered = shutil.which("taskctl")
    if discovered:
        return discovered
    user_install = Path.home() / ".local" / "bin" / "taskctl.cmd"
    if user_install.is_file():
        return str(user_install)
    return "taskctl"


@dataclass(slots=True)
class DashiTaskProvider:
    """v1.1 JSON contract; unavailable dashi never corrupts Workspace state."""

    project_id: str = "local"
    runner: CommandRunner = _default_runner
    executable: str | None = None

    def _json(self, *args: str) -> Any:
        command = (self.executable or _default_executable(), *args, "--json")
        output = self.runner(command)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise TaskProviderError("taskctl returned invalid JSON") from exc

    @staticmethod
    def _requirement_label(requirement_id: str) -> str:
        return f"requirement:{requirement_id}"

    def create_task(self, requirement_id: str, task: Task) -> Task:
        args = [
            "issue",
            "create",
            "--project",
            self.project_id,
            "--title",
            task.title,
            "--description",
            task.description,
            "--status",
            task.status,
            "--labels",
            ",".join(dict.fromkeys((*task.labels, self._requirement_label(requirement_id)))),
        ]
        if task.priority:
            args.extend(("--priority", task.priority))
        if task.worktree:
            if not task.branch:
                raise TaskProviderError("A worktree task requires its branch")
            args.extend(("--worktree-path", task.worktree, "--worktree-branch", task.branch))
        elif task.branch:
            args.extend(("--git-branch", task.branch))
        return _task(self._json(*args)["task"])

    def get_task(self, task_id: str) -> Task:
        return _task(self._json("issue", "get", task_id)["task"])

    def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
        payload = self._json("issue", "list", "--project", self.project_id)
        required_label = self._requirement_label(requirement_id)
        return tuple(_task(item) for item in payload["tasks"] if required_label in item.get("labels", ()))

    def update_status(self, task_id: str, status: str) -> Task:
        current = self.get_task(task_id)
        args = ["issue", "move", task_id, "--status", status]
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        return _task(self._json(*args)["task"])

    def add_comment(self, task_id: str, body: str) -> None:
        self._json("comment", "add", task_id, "--body", body)

    def link_session(
        self,
        task_id: str,
        session_id: str,
        *,
        codex_project_id: str | None = None,
        codex_project_kind: str | None = None,
        codex_host_id: str | None = None,
        workspace_path: str | None = None,
    ) -> None:
        current = self.get_task(task_id)
        args = [
            "issue",
            "move",
            task_id,
            "--status",
            current.status,
            "--binding-thread-id",
            session_id,
        ]
        binding = (codex_project_id, codex_project_kind, codex_host_id, workspace_path)
        if any(binding) and not all(binding):
            raise TaskProviderError("A complete Codex task binding requires all four identity fields")
        if all(binding):
            args.extend(
                (
                    "--binding-codex-project-id",
                    str(codex_project_id),
                    "--binding-codex-project-kind",
                    str(codex_project_kind),
                    "--binding-codex-host-id",
                    str(codex_host_id),
                    "--binding-workspace-path",
                    str(workspace_path),
                )
            )
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        self._json(*args)

    def set_git_context(
        self, task_id: str, branch: str | None = None, worktree: str | None = None
    ) -> None:
        if worktree and not branch:
            raise TaskProviderError("A worktree binding requires its branch")
        if not branch:
            raise TaskProviderError("Git context requires a branch or worktree")
        current = self.get_task(task_id)
        args = ["issue", "update", task_id]
        if worktree:
            args.extend(("--worktree-path", worktree, "--worktree-branch", branch))
        else:
            args.extend(("--git-branch", branch))
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        self._json(*args)
