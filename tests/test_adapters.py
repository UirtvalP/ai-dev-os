import json
import os
import subprocess
from pathlib import Path

import pytest

from workspace_orchestrator.adapters import task as task_adapter
from workspace_orchestrator.adapters.agent import CodexAgentProvider
from workspace_orchestrator.adapters.git import LocalGitProvider
from workspace_orchestrator.adapters.task import DashiTaskProvider, TaskProviderError
from workspace_orchestrator.context import build_snapshot, handoff
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
    _git(tmp_path, "commit", "-m", "中文提交")
    tracked.write_text("changed\n", encoding="utf-8")

    provider = LocalGitProvider(tmp_path)

    assert provider.current_branch() in {"main", "master"}
    assert provider.status()["clean"] is False
    commits = provider.recent_commits()
    assert len(commits) == 1
    assert commits[0].endswith(" 中文提交")
    assert "changed" in provider.diff()


def test_local_git_provider_reports_changed_and_untracked_files(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    tracked.write_text("changed\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    assert LocalGitProvider(tmp_path).changed_files() == ("tracked.txt", "new.txt")


def test_codex_agent_provider_owns_thread_environment_lookup() -> None:
    provider = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-123"})

    assert provider.name == "codex"
    assert provider.current_session_id() == "thread-123"


def test_resume_prefers_requirement_bound_worktree_over_current_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    worktree = tmp_path / ".worktrees" / "REQ-001"
    _git(tmp_path, "worktree", "add", "-b", "feature/REQ-001", str(worktree))
    tracked.write_text("dirty root\n", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Bound worktree")
    store.touch_meta(
        requirement_id,
        git={"branch": "feature/REQ-001", "worktree": ".worktrees/REQ-001"},
    )

    snapshot = build_snapshot(store, requirement_id)

    assert "- 分支：feature/REQ-001" in snapshot
    assert "- 工作树：.worktrees/REQ-001" in snapshot
    assert "- 状态：干净" in snapshot
    assert store.load(requirement_id)["meta"]["git"]["worktree"] == ".worktrees/REQ-001"


def test_resume_never_replaces_unavailable_bound_worktree_with_repo_root(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Missing bound worktree")
    binding = {"branch": "feature/missing", "worktree": ".worktrees/missing"}
    store.touch_meta(requirement_id, git=binding)

    snapshot = build_snapshot(store, requirement_id)

    assert "- 分支：feature/missing" in snapshot
    assert "- 工作树：.worktrees/missing" in snapshot
    assert "- 状态：不可用" in snapshot
    assert store.load(requirement_id)["meta"]["git"] == binding


def test_resume_links_active_task_to_session_on_both_sides(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Linked task")
    links: list[tuple[str, str]] = []

    class Tasks:
        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            return (Task(id="TASK-001", title="Fix bug", status="in_progress"),)

        def link_session(self, task_id: str, session_id: str, **_: object) -> None:
            links.append((task_id, session_id))

    build_snapshot(
        store,
        requirement_id,
        task_provider=Tasks(),  # type: ignore[arg-type]
        agent_provider=CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"}),
    )

    assert store.load(requirement_id)["sessions"][0]["task_ids"] == ["TASK-001"]
    assert links == [("TASK-001", "thread-a")]


def test_repeated_resume_does_not_repeat_session_or_task_attach(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Idempotent attach")
    links: list[tuple[str, str]] = []

    class Tasks:
        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            return (Task(id="TASK-001", title="Fix bug", status="in_progress"),)

        def link_session(self, task_id: str, session_id: str, **_: object) -> None:
            links.append((task_id, session_id))

    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"})
    for _ in range(2):
        build_snapshot(
            store,
            requirement_id,
            task_provider=Tasks(),  # type: ignore[arg-type]
            agent_provider=agent,
        )

    assert len(store.load(requirement_id)["sessions"]) == 1
    assert links == [("TASK-001", "thread-a")]


def test_handoff_collects_changed_files_from_requirement_git_context(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    tracked.write_text("changed\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Automatic handoff files")

    handoff(store, requirement_id)

    document = store.load(requirement_id)["handoff"]
    assert "- tracked.txt" in document
    assert "- new.txt" in document


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


def test_dashi_adapter_starts_service_once_before_first_command() -> None:
    starts: list[str] = []

    def runner(command: tuple[str, ...]) -> str:
        return json.dumps({"tasks": []})

    provider = DashiTaskProvider(
        runner=runner,
        executable="taskctl",
        service_starter=lambda: starts.append("started"),
    )

    provider.list_tasks("REQ-001")
    provider.list_tasks("REQ-001")

    assert starts == ["started"]


def test_taskboard_service_is_started_when_local_port_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = ".cmd" if os.name == "nt" else ""
    launcher = tmp_path / f"dashi-taskboard{suffix}"
    launcher.write_text("launcher", encoding="utf-8")
    checks = iter((False, True))
    launches: list[tuple[object, dict[str, object]]] = []

    monkeypatch.setenv("CODEX_TASKBOARD_URL", "http://127.0.0.1:47999")
    monkeypatch.setattr(task_adapter, "_taskboard_launcher", lambda: launcher)
    monkeypatch.setattr(
        task_adapter, "_service_is_listening", lambda host, port: next(checks)
    )
    monkeypatch.setattr(
        task_adapter.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append((command, kwargs)),
    )

    task_adapter.ensure_taskboard_service()

    assert len(launches) == 1
    assert launches[0][1]["env"]["CODEX_TASKBOARD_PORT"] == "47999"


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


def test_dashi_unlink_clears_only_matching_current_thread_binding() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> str:
        commands.append(tuple(command))
        return json.dumps(
            {
                "task": {
                    "id": "opaque-1",
                    "identifier": "AID-1",
                    "title": "Login",
                    "status": "in_progress",
                    "threadId": "thread-a",
                    "version": 4,
                }
            }
        )

    provider = DashiTaskProvider(runner=runner, executable="taskctl")
    provider.unlink_session("AID-1", "thread-a")

    assert commands[-1] == (
        "taskctl",
        "issue",
        "move",
        "AID-1",
        "--status",
        "in_progress",
        "--clear-binding-thread",
        "--if-version",
        "4",
        "--json",
    )


def test_dashi_adapter_rejects_non_json() -> None:
    with pytest.raises(TaskProviderError, match="无效 JSON"):
        DashiTaskProvider(runner=lambda _: "not-json").list_tasks("REQ-001")


def test_snapshot_degrades_when_task_provider_is_unavailable(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Demo")

    class UnavailableProvider:
        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            raise TaskProviderError("offline")

    snapshot = build_snapshot(store, requirement_id, task_provider=UnavailableProvider())  # type: ignore[arg-type]

    assert "任务：\n不可用（offline）" in snapshot
