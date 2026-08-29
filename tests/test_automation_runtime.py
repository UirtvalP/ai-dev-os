from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from workspace_orchestrator.adapters.agent import CodexAgentProvider
from workspace_orchestrator.automation.requirement_attach import discover_project_root
from workspace_orchestrator.automation.runtime import AutomationRuntime
from workspace_orchestrator.automation.task_attach import configured_task_provider
from workspace_orchestrator.cli import main
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceStore


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _init_git(path: Path) -> None:
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("Example: hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


class FakeTasks:
    def __init__(self) -> None:
        self.tasks: dict[str, list[Task]] = {}
        self.created: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.unlinks: list[tuple[str, str]] = []
        self.git_updates: list[tuple[str, str | None, str | None]] = []

    def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
        return tuple(self.tasks.get(requirement_id, ()))

    def create_task(self, requirement_id: str, task: Task) -> Task:
        created = replace(task, id=f"TASK-{len(self.created) + 1:03d}")
        self.tasks.setdefault(requirement_id, []).append(created)
        self.created.append(created.id)
        return created

    def get_task(self, task_id: str) -> Task:
        return self._find(task_id)[2]

    def update_status(self, task_id: str, status: str) -> Task:
        requirement_id, index, task = self._find(task_id)
        updated = replace(task, status=status)
        self.tasks[requirement_id][index] = updated
        return updated

    def link_session(self, task_id: str, session_id: str, **_: object) -> None:
        self.links.append((task_id, session_id))

    def unlink_session(self, task_id: str, session_id: str) -> None:
        self.unlinks.append((task_id, session_id))

    def set_git_context(
        self, task_id: str, branch: str | None = None, worktree: str | None = None
    ) -> None:
        requirement_id, index, task = self._find(task_id)
        self.tasks[requirement_id][index] = replace(task, branch=branch, worktree=worktree)
        self.git_updates.append((task_id, branch, worktree))

    def _find(self, task_id: str) -> tuple[str, int, Task]:
        for requirement_id, tasks in self.tasks.items():
            for index, task in enumerate(tasks):
                if task.id == task_id:
                    return requirement_id, index, task
        raise AssertionError(f"unknown task: {task_id}")


def test_black_box_a_and_b_bootstrap_then_repeat_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """场景 A/B：薄触发器只调用 bootstrap，后续链路全由 Runtime 完成。"""

    _init_git(tmp_path)
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create(
        "唯一活动需求", task_provider="dashi", task_project_id="demo"
    )
    tasks = FakeTasks()
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    monkeypatch.setattr(
        "workspace_orchestrator.automation.runtime.ensure_project_task_services",
        lambda store: None,
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.task_attach.DashiTaskProvider",
        lambda project_id, service_starter: tasks,
    )
    request = "把 README 中的默认示例改成中文。"

    assert main(["--root", str(tmp_path), "bootstrap", "--request", request]) == 0
    first_snapshot = capsys.readouterr().out
    assert main(["--root", str(tmp_path), "bootstrap", "--request", request]) == 0
    second_snapshot = capsys.readouterr().out

    assert tasks.created == ["TASK-001"]
    assert tasks.get_task("TASK-001").status == "in_progress"
    assert tasks.links == [("TASK-001", "thread-a")]
    assert len(tasks.git_updates) == 1
    assert store.attached_requirement_id("thread-a") == requirement_id
    assert len(store.load(requirement_id)["sessions"]) == 1
    assert "README" in first_snapshot
    assert "TASK-001 [in_progress" in second_snapshot
    assert "Git：" in first_snapshot and "最近提交" in first_snapshot


def test_configured_dashi_provider_automatically_ensures_taskboard_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create(
        "自动启动任务面板", task_provider="dashi", task_project_id="demo"
    )
    captured: dict[str, object] = {}
    tasks = FakeTasks()

    def factory(*, project_id: str, service_starter: object) -> FakeTasks:
        captured.update(project_id=project_id, service_starter=service_starter)
        return tasks

    monkeypatch.setattr(
        "workspace_orchestrator.automation.task_attach.DashiTaskProvider", factory
    )

    provider = configured_task_provider(store.load(requirement_id)["meta"])

    assert provider is tasks
    assert captured["project_id"] == "demo"
    assert callable(captured["service_starter"])
    assert captured["service_starter"].__name__ == "ensure_taskboard_service"  # type: ignore[union-attr]


def test_existing_session_task_binding_wins_over_later_explicit_task(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Bound task")
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(id="TASK-001", title="Current", status="in_progress"),
        Task(id="TASK-002", title="Other", status="todo"),
    ]
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-bound"}),
        tasks,  # type: ignore[arg-type]
    )
    runtime.bootstrap(requirement_id, task_ids=("TASK-001",))

    runtime.bootstrap(requirement_id, task_ids=("TASK-002",))

    session = store.load(requirement_id)["sessions"][0]
    assert session["task_ids"] == ["TASK-001"]
    assert tasks.get_task("TASK-002").status == "todo"


def test_black_box_c_multiple_requirements_returns_ambiguity_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """场景 C：多个活动 Requirement 只能返回 ambiguity。"""

    store = WorkspaceStore(tmp_path)
    store.create("First")
    store.create("Second")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-c")

    assert main(["--root", str(tmp_path), "bootstrap", "--request", "修 README"]) == 2

    error = capsys.readouterr().err
    assert "状态：ambiguity" in error
    assert "REQ-001, REQ-002" in error
    assert all(store.load(item)["sessions"] == [] for item in ("REQ-001", "REQ-002"))


def test_finalize_runs_known_verification_and_automates_review_handoff_detach(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.workspace-orchestrator.automation]\n"
        "verification-commands = [\n"
        '  ["{python}", "-c", "print(\'pytest\')"],\n'
        '  ["{python}", "-c", "print(\'ruff\')"],\n'
        '  ["{python}", "-c", "print(\'integration\')"],\n'
        "]\n",
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Finalize")
    tasks = FakeTasks()
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-final"})
    runtime = AutomationRuntime(store, agent, tasks)  # type: ignore[arg-type]
    runtime.bootstrap(requirement_id, development_request="修改 README")
    (tmp_path / "README.md").write_text("示例：你好\n", encoding="utf-8")

    result = runtime.finalize(requirement_id, completed=("README 中文示例",))

    data = store.load(requirement_id)
    assert result.passed is True
    assert tasks.get_task("TASK-001").status == "in_review"
    assert tasks.unlinks == [("TASK-001", "thread-final")]
    assert data["sessions"][0]["result"] == "completed"
    assert "- README.md" in data["handoff"]
    assert "状态：PASS" in data["verification"]
    assert "状态：TODO" not in data["verification"]
    assert data["meta"]["status"] != "done"


def test_project_discovery_walks_up_to_workspace(tmp_path: Path) -> None:
    WorkspaceStore(tmp_path).create("Discover")
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)

    assert discover_project_root(nested) == tmp_path


def test_repo_hook_config_and_script_drive_session_lifecycle(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    hook_config = json.loads((repo_root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert set(hook_config["hooks"]) == {"SessionStart", "UserPromptSubmit", "SessionEnd"}
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Hook lifecycle")
    script = repo_root / ".codex" / "hooks" / "workspace_runtime.py"
    submit = {
        "session_id": "hook-thread",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "turn_id": "turn-1",
        "prompt": f"继续 {requirement_id}",
    }

    started = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(submit, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    payload = json.loads(started.stdout)
    assert requirement_id in payload["hookSpecificOutput"]["additionalContext"]
    assert store.attached_requirement_id("hook-thread") == requirement_id

    ended = {**submit, "hook_event_name": "SessionEnd", "reason": "other"}
    subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(ended, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert store.attached_requirement_id("hook-thread") is None
    assert store.load(requirement_id)["sessions"][0]["result"] == "detached"
