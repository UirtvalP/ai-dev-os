import json
import multiprocessing
import time
from pathlib import Path

import pytest

from workspace_orchestrator.adapters.agent import CodexAgentProvider
from workspace_orchestrator.cli import main
from workspace_orchestrator.context import bootstrap_session, build_snapshot, checkpoint, handoff
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def _concurrent_workspace_update(root: str, requirement_id: str, index: int) -> None:
    from pathlib import Path

    from workspace_orchestrator.automation.session_runtime import attach_session
    from workspace_orchestrator.automation.state_sync import persist_checkpoint
    from workspace_orchestrator.workspace import WorkspaceStore

    store = WorkspaceStore(Path(root))
    persist_checkpoint(store, requirement_id, completed=(f"并发事项 {index}",))
    attach_session(
        store,
        requirement_id,
        session_id=f"thread-{index}",
        agent_name="codex",
    )


def _concurrent_create(root: str, index: int) -> None:
    WorkspaceStore(Path(root)).create(f"并发需求 {index}")


def _hold_requirement_lock(root: str, requirement_id: str, ready: object) -> None:
    store = WorkspaceStore(Path(root))
    with store.locked(requirement_id):
        ready.set()  # type: ignore[attr-defined]
        time.sleep(30)


def test_create_initializes_complete_human_readable_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)

    first = store.create("Add authentication", acceptance=["Valid users can log in"])
    second = store.create("Add audit log")

    assert first == "REQ-001"
    assert second == "REQ-002"
    data = store.load(first)
    assert data["meta"]["id"] == first
    assert data["meta"]["title"] == "Add authentication"
    assert data["meta"]["task_provider"] == "dashi"
    assert data["meta"]["task_project_id"]
    assert "- [ ] Valid users can log in" in data["requirement"]
    assert data["sessions"] == []
    assert {path.name for path in data["path"].iterdir()} == {
        "meta.json",
        "requirement.md",
        "intent.md",
        "state.md",
        "plan.md",
        "decisions.md",
        "verification.md",
        "handoff.md",
        "sessions.json",
    }


def test_load_rejects_incomplete_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    path = store.path_for("REQ-001")
    path.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="不完整"):
        store.load("REQ-001")


def test_load_migrates_legacy_workspace_without_overwriting_files(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Legacy requirement", goal="Preserve old state")
    intent_path = store.path_for(requirement_id) / "intent.md"
    intent_path.unlink()

    data = store.load(requirement_id)

    assert intent_path.is_file()
    assert "Preserve old state" in data["intent"]
    assert "迁移到意图层之前" in data["intent"]
    assert "- 需求意图：PARTIAL" in data["intent"]


def test_load_migrates_legacy_null_provider_but_preserves_explicit_disable(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    legacy = store.create("Legacy provider")
    disabled = store.create("Explicit local only", task_provider=None)
    legacy_meta_path = store.path_for(legacy) / "meta.json"
    legacy_meta = store.read_json(legacy_meta_path)
    legacy_meta["task_provider"] = None
    legacy_meta["task_project_id"] = None
    legacy_meta.pop("task_provider_explicitly_disabled")
    store.write_json(legacy_meta_path, legacy_meta)

    assert store.load(legacy)["meta"]["task_provider"] == "dashi"
    assert store.load(disabled)["meta"]["task_provider"] is None


def test_current_id_resolves_only_active_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Only active requirement")

    assert store.current_id() == requirement_id


def test_current_id_refuses_to_guess_between_active_workspaces(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    store.create("First")
    store.create("Second")

    with pytest.raises(WorkspaceError, match="多个活动"):
        store.current_id()


def test_bootstrap_explicitly_attaches_then_reuses_requirement(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("First")
    store.create("Second")
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"})

    first_snapshot = bootstrap_session(store, first, agent_provider=agent)
    resumed_snapshot = bootstrap_session(store, agent_provider=agent)

    assert "REQ-001 First" in first_snapshot
    assert "REQ-001 First" in resumed_snapshot
    assert store.attached_requirement_id("thread-a") == first
    assert len(store.load(first)["sessions"]) == 1


def test_snapshot_keeps_explicit_local_task_without_external_provider(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Local task")

    build_snapshot(
        store,
        requirement_id,
        agent_provider=CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"}),
        task_ids=("LOCAL-TASK-1",),
    )

    assert store.load(requirement_id)["sessions"][0]["task_ids"] == ["LOCAL-TASK-1"]


def test_bootstrap_refuses_to_guess_without_session_or_unique_requirement(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    store.create("First")
    store.create("Second")

    with pytest.raises(WorkspaceError, match="CODEX_THREAD_ID"):
        bootstrap_session(store, agent_provider=CodexAgentProvider(environ={}))

    with pytest.raises(WorkspaceError, match="多个活动"):
        bootstrap_session(
            store,
            agent_provider=CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"}),
        )


def test_bootstrap_explicit_id_switches_without_leaving_ambiguous_attach(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("First")
    second = store.create("Second")
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"})
    bootstrap_session(store, first, agent_provider=agent)

    snapshot = bootstrap_session(store, second, agent_provider=agent)

    assert "REQ-002 Second" in snapshot
    assert store.load(first)["sessions"][0]["result"] == "detached"
    assert store.attached_requirement_id("thread-a") == second

    bootstrap_session(store, first, agent_provider=agent)

    first_session = store.load(first)["sessions"][0]
    assert first_session["result"] == "in_progress"
    assert first_session["ended_at"] is None
    assert store.load(second)["sessions"][0]["result"] == "detached"


class _BootstrapTasks:
    def __init__(self, tasks: dict[str, list[Task]]) -> None:
        self.tasks = tasks
        self.created: list[tuple[str, Task]] = []
        self.links: list[tuple[str, str]] = []
        self.unlinks: list[tuple[str, str]] = []

    def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
        return tuple(self.tasks.get(requirement_id, []))

    def create_task(self, requirement_id: str, task: Task) -> Task:
        created = Task(
            id=f"TASK-{len(self.created) + 1:03d}",
            title=task.title,
            description=task.description,
            status=task.status,
        )
        self.created.append((requirement_id, task))
        self.tasks.setdefault(requirement_id, []).append(created)
        return created

    def link_session(self, task_id: str, session_id: str, **_: object) -> None:
        self.links.append((task_id, session_id))

    def unlink_session(self, task_id: str, session_id: str) -> None:
        self.unlinks.append((task_id, session_id))


def test_bootstrap_creates_in_progress_task_from_development_request(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Task bootstrap")
    tasks = _BootstrapTasks({})
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"})

    snapshot = bootstrap_session(
        store,
        requirement_id,
        agent_provider=agent,
        task_provider=tasks,  # type: ignore[arg-type]
        development_request="修复 Bootstrap 的 Task 绑定语义",
    )

    assert tasks.created[0][0] == requirement_id
    assert tasks.created[0][1].status == "in_progress"
    assert tasks.links == [("TASK-001", "thread-a")]
    assert store.load(requirement_id)["sessions"][0]["task_ids"] == ["TASK-001"]
    assert "TASK-001 [in_progress" in snapshot


def test_bootstrap_refuses_multiple_active_tasks_before_switching_requirement(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("First")
    second = store.create("Second")
    tasks = _BootstrapTasks(
        {
            first: [Task(id="TASK-001", title="First", status="in_progress")],
            second: [
                Task(id="TASK-002", title="Second A", status="in_progress"),
                Task(id="TASK-003", title="Second B", status="in_progress"),
            ],
        }
    )
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"})
    bootstrap_session(store, first, agent_provider=agent, task_provider=tasks)  # type: ignore[arg-type]

    with pytest.raises(WorkspaceError, match="多个 in_progress Task"):
        bootstrap_session(
            store,
            second,
            agent_provider=agent,
            task_provider=tasks,  # type: ignore[arg-type]
        )

    assert store.attached_requirement_id("thread-a") == first
    assert tasks.unlinks == []


def test_bootstrap_switch_clears_old_current_task_binding_and_binds_new_task(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("First")
    second = store.create("Second")
    tasks = _BootstrapTasks(
        {
            first: [Task(id="TASK-001", title="First", status="in_progress")],
            second: [Task(id="TASK-002", title="Second", status="in_progress")],
        }
    )
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"})
    bootstrap_session(store, first, agent_provider=agent, task_provider=tasks)  # type: ignore[arg-type]

    bootstrap_session(store, second, agent_provider=agent, task_provider=tasks)  # type: ignore[arg-type]

    assert tasks.unlinks == [("TASK-001", "thread-a")]
    assert tasks.links == [("TASK-001", "thread-a"), ("TASK-002", "thread-a")]
    assert store.load(first)["sessions"][0]["result"] == "detached"
    assert store.load(first)["sessions"][0]["task_ids"] == ["TASK-001"]
    assert store.load(second)["sessions"][0]["task_ids"] == ["TASK-002"]

    bootstrap_session(store, first, agent_provider=agent, task_provider=tasks)  # type: ignore[arg-type]

    assert tasks.unlinks[-1] == ("TASK-002", "thread-a")
    assert tasks.links[-1] == ("TASK-001", "thread-a")
    assert store.load(first)["sessions"][0]["result"] == "in_progress"


def test_bootstrap_validates_explicit_target_task_before_detaching_old_session(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("First")
    second = store.create("Second")
    tasks = _BootstrapTasks(
        {
            first: [Task(id="TASK-001", title="First", status="in_progress")],
            second: [Task(id="TASK-002", title="Second", status="todo")],
        }
    )
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-a"})
    bootstrap_session(store, first, agent_provider=agent, task_provider=tasks)  # type: ignore[arg-type]

    with pytest.raises(WorkspaceError, match="不属于需求"):
        bootstrap_session(
            store,
            second,
            agent_provider=agent,
            task_provider=tasks,  # type: ignore[arg-type]
            task_ids=("TASK-999",),
        )

    assert store.attached_requirement_id("thread-a") == first
    assert tasks.unlinks == []


def test_checkpoint_and_handoff_restore_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Add authentication", goal="Implement JWT authentication")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    agent_provider = CodexAgentProvider()

    checkpoint(
        store,
        requirement_id,
        phase="implementation",
        completed=["Login endpoint"],
        next_action="Implement middleware",
        verification="pytest tests/auth: PASS",
        agent_provider=agent_provider,
    )
    handoff(
        store,
        requirement_id,
        completed=["Login endpoint"],
        files_changed=["src/auth/login.py"],
        current_state="Login works; middleware is pending.",
        important_context="Keep auth logic in the auth module.",
        next_action="Implement middleware",
        agent_provider=agent_provider,
    )

    monkeypatch.setenv("CODEX_THREAD_ID", "thread-b")
    snapshot = build_snapshot(store, requirement_id, agent_provider=agent_provider)
    checkpoint(store, requirement_id, agent_provider=agent_provider)
    data = store.load(requirement_id)

    assert "REQ-001 Add authentication" in snapshot
    assert "Implement JWT authentication" in snapshot
    assert "- Login endpoint" in snapshot
    assert "Login works; middleware is pending." in snapshot
    assert "Implement middleware" in snapshot
    assert data["meta"]["status"] == "in_progress"
    assert [session["id"] for session in data["sessions"]] == ["thread-a", "thread-b"]
    assert data["sessions"][0]["result"] == "completed"


def test_resume_restores_concise_intent_summaries(tmp_path: Path) -> None:
    from workspace_orchestrator.user_config import user_principles_path

    principles = user_principles_path()
    principles.parent.mkdir(parents=True)
    principles.write_text(
        "# User Principles\n\n## Execute Small Work\n\n"
        "Summary: Make local fixes directly.\n\n"
        "This long explanation must not be copied into the snapshot.\n",
        encoding="utf-8",
    )
    (tmp_path / "PROJECT_INTENT.md").write_text(
        "# Project Intent\n\n## Purpose\n\n"
        "Summary: Restore intent across sessions.\n\n"
        "This project-level detail must not be copied into the snapshot.\n",
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Intent restore", goal="Keep the why")
    data = store.load(requirement_id)
    intent = data["intent"].replace("说明此需求为何重要。", "避免交接后重复分析。")
    store.write_text(data["path"] / "intent.md", intent)

    snapshot = build_snapshot(store, requirement_id)

    assert "用户原则：\n- Execute Small Work：Make local fixes directly." in snapshot
    assert "项目意图：\n- 目的：Restore intent across sessions." in snapshot
    assert "需求意图：\n- 原因：避免交接后重复分析。" in snapshot
    assert "long explanation" not in snapshot
    assert "project-level detail" not in snapshot


def test_resume_ignores_legacy_project_principles(tmp_path: Path) -> None:
    from workspace_orchestrator.user_config import user_principles_path

    principles = user_principles_path()
    principles.parent.mkdir(parents=True)
    principles.write_text(
        "# User Principles\n\n## Global\n\nSummary: Use global principles.\n",
        encoding="utf-8",
    )
    (tmp_path / "USER_PRINCIPLES.md").write_text(
        "# User Principles\n\n## Legacy\n\nSummary: Use project principles.\n",
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Global principles")

    snapshot = build_snapshot(store, requirement_id)

    assert "Global：Use global principles." in snapshot
    assert "project principles" not in snapshot


def test_cli_lifecycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root_args = ["--root", str(tmp_path)]
    assert main([*root_args, "new", "CLI demo", "--acceptance", "Snapshot is concise"]) == 0
    assert "已创建 REQ-001" in capsys.readouterr().out

    assert main([*root_args, "checkpoint", "REQ-001", "--phase", "implementation"]) == 0
    assert main([*root_args, "resume", "REQ-001"]) == 0
    output = capsys.readouterr().out
    assert "# 工作区上下文" in output
    assert "当前阶段：\nimplementation" in output

    assert main([*root_args, "status", "REQ-001"]) == 0
    assert "状态：in_progress" in capsys.readouterr().out

    assert main([*root_args, "current"]) == 0
    assert capsys.readouterr().out.strip() == "REQ-001"
    assert main([*root_args, "resume"]) == 0
    assert "REQ-001 CLI demo" in capsys.readouterr().out


def test_cli_confirm_requires_explicit_flag_and_marks_only_requirement_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Confirm", task_provider=None)
    store.touch_meta(requirement_id, status="in_review")
    root_args = ["--root", str(tmp_path)]

    assert main([*root_args, "confirm", requirement_id]) == 2
    assert "明确确认" in capsys.readouterr().err
    assert store.load(requirement_id)["meta"]["status"] == "in_review"

    assert main([*root_args, "confirm", requirement_id, "--user-confirmed"]) == 0
    assert "外部 Task 未自动完成" in capsys.readouterr().out
    assert store.load(requirement_id)["meta"]["status"] == "done"


def test_cli_bootstrap_auto_attaches_unique_requirement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    store.create("Bootstrap demo")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-cli")

    assert main(["--root", str(tmp_path), "bootstrap"]) == 0

    assert "REQ-001 Bootstrap demo" in capsys.readouterr().out
    assert store.attached_requirement_id("thread-cli") == "REQ-001"


def test_persisted_json_is_utf8_and_readable(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("中文需求")
    meta_path = store.path_for(requirement_id) / "meta.json"

    assert json.loads(meta_path.read_text(encoding="utf-8"))["title"] == "中文需求"


def test_concurrent_sessions_preserve_json_meta_and_markdown_updates(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Concurrent updates")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_concurrent_workspace_update,
            args=(str(tmp_path), requirement_id, index),
        )
        for index in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    data = store.load(requirement_id)
    assert {item["id"] for item in data["sessions"]} == {f"thread-{index}" for index in range(6)}
    assert all(f"- 并发事项 {index}" in data["state"] for index in range(6))
    assert all(f"- [x] 并发事项 {index}" in data["plan"] for index in range(6))
    assert data["meta"]["status"] == "draft"


def test_concurrent_create_allocates_unique_complete_requirement_ids(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_concurrent_create, args=(str(tmp_path), index))
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    store = WorkspaceStore(tmp_path)
    assert store.requirement_ids() == tuple(f"REQ-{index:03d}" for index in range(1, 9))
    assert all(store.load(item)["meta"]["id"] == item for item in store.requirement_ids())


def test_dead_process_releases_requirement_lock_immediately(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("锁恢复")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_hold_requirement_lock, args=(str(tmp_path), requirement_id, ready)
    )
    process.start()
    assert ready.wait(10)
    process.terminate()
    process.join(10)
    started = time.monotonic()
    with store.locked(requirement_id):
        store.touch_meta(requirement_id, recovered=True)
    assert time.monotonic() - started < 3
    assert store.load(requirement_id)["meta"]["recovered"] is True


def test_cli_can_configure_dashi_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(
        [
            "--root",
            str(tmp_path),
            "new",
            "Integrated task",
            "--task-provider",
            "dashi",
            "--task-project",
            "ai-dev-os",
        ]
    )

    assert result == 0
    capsys.readouterr()
    meta = WorkspaceStore(tmp_path).load("REQ-001")["meta"]
    assert meta["task_provider"] == "dashi"
    assert meta["task_project_id"] == "ai-dev-os"


def test_cli_uses_dashi_by_default_and_allows_explicit_disable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "new", "Default provider"]) == 0
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "new",
                "Local only",
                "--no-task-provider",
            ]
        )
        == 0
    )
    capsys.readouterr()

    store = WorkspaceStore(tmp_path)
    assert store.load("REQ-001")["meta"]["task_provider"] == "dashi"
    assert store.load("REQ-002")["meta"]["task_provider"] is None


def test_cli_bootstrap_passes_current_development_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, object] = {}

    def fake_bootstrap(*_: object, **kwargs: object) -> str:
        received.update(kwargs)
        return "snapshot"

    monkeypatch.setattr("workspace_orchestrator.cli.bootstrap_session", fake_bootstrap)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "bootstrap",
                "REQ-004",
                "--request",
                "完善 Task Bootstrap",
            ]
        )
        == 0
    )
    assert received["development_request"] == "完善 Task Bootstrap"


def test_cli_checkpoint_records_explicit_task_for_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Explicit task link")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-cli")

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "checkpoint",
                requirement_id,
                "--task",
                "TASK-007",
            ]
        )
        == 0
    )

    assert store.load(requirement_id)["sessions"][0]["task_ids"] == ["TASK-007"]
