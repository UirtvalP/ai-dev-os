import json
import subprocess
from pathlib import Path

import pytest

from workspace_orchestrator.adapters.git import LocalGitProvider
from workspace_orchestrator.adapters.task import DashiTaskProvider, TaskProviderError
from workspace_orchestrator.context import build_snapshot
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceStore


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def test_local_git_provider_reports_repository_state(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    tracked.write_text("changed\n", encoding="utf-8")

    provider = LocalGitProvider(tmp_path)

    assert provider.current_branch() in {"main", "master"}
    assert provider.status()["clean"] is False
    commits = provider.recent_commits()
    assert len(commits) == 1
    assert commits[0].endswith(" initial")
    assert "changed" in provider.diff()


def test_dashi_adapter_uses_json_contract() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> str:
        commands.append(tuple(command))
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "opaque-1",
                        "identifier": "AID-1",
                        "projectId": "ai-dev-os",
                        "title": "Login",
                        "status": "todo",
                        "labels": ["requirement:REQ-001"],
                        "version": 3,
                    },
                    {
                        "id": "opaque-2",
                        "identifier": "AID-2",
                        "title": "Other requirement",
                        "labels": ["requirement:REQ-999"],
                    },
                ]
            }
        )

    tasks = DashiTaskProvider(
        project_id="ai-dev-os", runner=runner, executable="taskctl"
    ).list_tasks("REQ-001")

    assert tasks == (
        Task(
            id="AID-1",
            raw_id="opaque-1",
            project_id="ai-dev-os",
            title="Login",
            labels=("requirement:REQ-001",),
            version=3,
        ),
    )
    assert commands == [("taskctl", "issue", "list", "--project", "ai-dev-os", "--json")]


def test_dashi_adapter_creates_requirement_linked_issue() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> str:
        commands.append(tuple(command))
        return json.dumps(
            {
                "task": {
                    "id": "opaque-1",
                    "identifier": "AID-1",
                    "title": "Login",
                    "labels": ["backend", "requirement:REQ-001"],
                }
            }
        )

    created = DashiTaskProvider(
        project_id="ai-dev-os", runner=runner, executable="taskctl"
    ).create_task(
        "REQ-001",
        Task(id="new", title="Login", labels=("backend",), branch="main"),
    )

    assert created.id == "AID-1"
    assert commands[0] == (
        "taskctl",
        "issue",
        "create",
        "--project",
        "ai-dev-os",
        "--title",
        "Login",
        "--description",
        "",
        "--status",
        "todo",
        "--labels",
        "backend,requirement:REQ-001",
        "--git-branch",
        "main",
        "--json",
    )


def test_dashi_status_update_uses_current_version() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> str:
        commands.append(tuple(command))
        status = "in_review" if "move" in command else "in_progress"
        version = 5 if "move" in command else 4
        return json.dumps(
            {
                "task": {
                    "id": "opaque-1",
                    "identifier": "AID-1",
                    "title": "Login",
                    "status": status,
                    "version": version,
                }
            }
        )

    updated = DashiTaskProvider(runner=runner, executable="taskctl").update_status(
        "AID-1", "in_review"
    )

    assert updated.status == "in_review"
    assert commands[-1] == (
        "taskctl",
        "issue",
        "move",
        "AID-1",
        "--status",
        "in_review",
        "--if-version",
        "4",
        "--json",
    )


def test_dashi_adapter_rejects_non_json() -> None:
    with pytest.raises(TaskProviderError, match="invalid JSON"):
        DashiTaskProvider(runner=lambda _: "not-json").list_tasks("REQ-001")


def test_snapshot_degrades_when_task_provider_is_unavailable(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo")

    class UnavailableProvider:
        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            raise TaskProviderError("offline")

    snapshot = build_snapshot(store, requirement_id, task_provider=UnavailableProvider())  # type: ignore[arg-type]

    assert "Tasks:\nUnavailable (offline)" in snapshot
