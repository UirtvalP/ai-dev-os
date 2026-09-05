import json
import subprocess
from pathlib import Path

import pytest

from workspace_orchestrator.adapters.agent import CodexAgentProvider, CodexExecProvider
from workspace_orchestrator.adapters.git import LocalGitProvider
from workspace_orchestrator.adapters.package import ToolInstallerError, UvToolInstaller
from workspace_orchestrator.adapters.task import DashiTaskProvider, TaskProviderError
from workspace_orchestrator.context import build_snapshot, handoff
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceStore


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def test_uv_tool_installer_reinstalls_global_cli_from_explicit_source() -> None:
    commands: list[list[str]] = []

    def runner(command):
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="Installed ai-dev-os", stderr="")

    result = UvToolInstaller(
        executable="uv-test", runner=runner, platform="posix"
    ).upgrade("local-source")

    assert commands == [
        ["uv-test", "tool", "install", "--force", "--refresh", "--", "local-source"]
    ]
    assert result.source == "local-source"
    assert result.details == "Installed ai-dev-os"


def test_uv_tool_installer_reports_command_failure() -> None:
    def runner(command):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="source missing")

    with pytest.raises(ToolInstallerError, match="source missing"):
        UvToolInstaller(
            executable="uv-test", runner=runner, platform="posix"
        ).upgrade("broken-source")


def test_uv_tool_installer_schedules_windows_update_after_cli_exit(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    result_path = tmp_path / "upgrade.log"

    def scheduler(executable: str, source: str) -> Path:
        calls.append((executable, source))
        return result_path

    result = UvToolInstaller(
        executable="uv-test",
        scheduler=scheduler,
        platform="nt",
    ).upgrade("local-source")

    assert calls == [("uv-test", "local-source")]
    assert result.scheduled is True
    assert result.result_path == result_path


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
    archived: list[str] = []
    provider = CodexAgentProvider(
        environ={"CODEX_THREAD_ID": "thread-123"}, archive_runner=archived.append
    )

    assert provider.name == "codex"
    assert provider.current_session_id() == "thread-123"
    provider.archive_session("thread-123")
    assert archived == ["thread-123"]


def test_codex_exec_provider_uses_official_non_interactive_boundary(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], Path, dict[str, str], float]] = []

    def runner(command, cwd, environ, timeout):
        calls.append((tuple(command), cwd, dict(environ), timeout))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-auto"}\n',
            stderr="",
        )

    result = CodexExecProvider(
        runner=runner, executable="codex", timeout_seconds=30
    ).execute(
        tmp_path,
        "继续 REQ-001，处理 AID-1",
        model="gpt-test",
        bypass_hook_trust=True,
    )

    assert result.session_id == "thread-auto"
    assert result.resumed is False
    command, cwd, environ, timeout = calls[0]
    assert command[:3] == ("codex", "exec", "--json")
    assert ("--sandbox", "workspace-write") == (
        command[command.index("--sandbox")],
        command[command.index("--sandbox") + 1],
    )
    assert 'approval_policy="never"' in command
    assert ("--model", "gpt-test") == (
        command[command.index("--model")],
        command[command.index("--model") + 1],
    )
    assert "--dangerously-bypass-hook-trust" in command
    assert cwd == tmp_path
    assert timeout == 30
    assert "CODEX_THREAD_ID" not in environ


def test_codex_exec_provider_falls_back_to_new_session_when_resume_fails(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command, cwd, environ, timeout):
        commands.append(tuple(command))
        if "resume" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"thread.started","thread_id":"thread-new"}\n',
            stderr="",
        )

    result = CodexExecProvider(runner=runner, executable="codex").execute(
        tmp_path,
        "继续",
        resume_session_id="thread-old",
    )

    assert commands[0][:3] == ("codex", "exec", "resume")
    assert commands[1][:2] == ("codex", "exec")
    assert result.session_id == "thread-new"
    assert result.resumed is False


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


def test_dashi_v2_conversation_attribution_is_not_an_active_thread_binding() -> None:
    """真实 schema v2 create payload 的顶层 Thread 归属不得阻止 Dispatcher。"""

    payload = {
        "tasks": [
            {
                "id": "opaque-1",
                "identifier": "AID-1",
                "projectId": "ai-dev-os",
                "title": "由 Main 委派",
                "status": "in_progress",
                "labels": ["requirement:REQ-001"],
                "threadId": "thread-main",
                "threadBinding": None,
                "legacyLocalThreadId": "thread-main",
                "conversationRefs": [
                    {
                        "threadId": "thread-main",
                        "legacyLocal": True,
                        "source": "task",
                    }
                ],
                "version": 1,
            }
        ],
        "schemaVersion": 2,
    }
    provider = DashiTaskProvider(
        project_id="ai-dev-os",
        runner=lambda _: json.dumps(payload),
        executable="taskctl",
    )

    task = provider.list_tasks("REQ-001")[0]

    assert task.session_ids == ("thread-main",)
    assert task.binding_session_id is None


def test_dashi_v2_explicit_thread_binding_wins_over_conversation_attribution() -> None:
    payload = {
        "tasks": [
            {
                "id": "opaque-1",
                "identifier": "AID-1",
                "title": "运行中的 Worker",
                "status": "in_progress",
                "labels": ["requirement:REQ-001"],
                "threadId": "thread-main",
                "legacyLocalThreadId": "thread-main",
                "threadBinding": {
                    "threadId": "thread-worker",
                    "codexProjectId": "project-1",
                    "codexProjectKind": "local",
                    "codexHostId": "host-1",
                    "workspacePath": "D:/code/AI Dev OS",
                },
            }
        ],
        "schemaVersion": 2,
    }
    provider = DashiTaskProvider(
        runner=lambda _: json.dumps(payload),
        executable="taskctl",
    )

    task = provider.list_tasks("REQ-001")[0]

    assert task.binding_session_id == "thread-worker"
    assert task.binding_codex_project_id == "project-1"
    assert task.binding_workspace_path == "D:/code/AI Dev OS"


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


def test_dashi_project_mapping_never_steals_same_id_from_another_workspace(
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    current = tmp_path / "current"
    other.mkdir()
    current.mkdir()

    def runner(command: tuple[str, ...]) -> str:
        assert command[1:3] == ("project", "list")
        return json.dumps(
            {
                "projects": [
                    {
                        "id": "same-project",
                        "name": "same-project",
                        "workspacePath": str(other),
                    }
                ]
            }
        )

    provider = DashiTaskProvider(
        project_id="same-project",
        runner=runner,
        executable="taskctl",
        workspace_path=str(current),
    )

    with pytest.raises(TaskProviderError, match="已映射到其他目录"):
        provider.list_tasks("REQ-001")


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


def test_dashi_review_packet_uses_description_file_cas_and_is_idempotent() -> None:
    commands: list[tuple[str, ...]] = []
    uploaded: list[str] = []
    current_description = "旧正文"

    def runner(command: tuple[str, ...]) -> str:
        nonlocal current_description
        commands.append(tuple(command))
        if "update" in command:
            file_path = Path(command[command.index("--description-file") + 1])
            uploaded.append(file_path.read_text(encoding="utf-8"))
            current_description = uploaded[-1]
            version = 5
        else:
            version = 4
        return json.dumps(
            {
                "task": {
                    "id": "opaque-1",
                    "identifier": "AID-1",
                    "title": "Review",
                    "description": current_description,
                    "status": "in_review",
                    "version": version,
                }
            }
        )

    provider = DashiTaskProvider(runner=runner, executable="taskctl")
    content = "<!-- packet -->\n\n完整审查材料"
    assert provider.publish_review("AID-1", content).description == content
    assert provider.publish_review("AID-1", content).description == content
    assert uploaded == [content]
    update = next(command for command in commands if "update" in command)
    assert update[-3:] == ("--if-version", "4", "--json")


def test_dashi_review_approval_fact_uses_last_done_transition_actor() -> None:
    provider = DashiTaskProvider(
        runner=lambda _: "{}",
        executable="taskctl",
        activity_reader=lambda _: {
            "activities": [
                {
                    "id": "change-1",
                    "actorType": "user",
                    "actorId": "alice",
                    "actorName": "Alice",
                    "createdAt": "2026-08-29T01:00:00Z",
                    "changes": [{"field": "status", "before": "in_review", "after": "done"}],
                },
                {
                    "id": "change-2",
                    "actorType": "agent",
                    "actorId": "codex-agent",
                    "actorName": "Codex Agent",
                    "createdAt": "2026-08-29T02:00:00Z",
                    "changes": [{"field": "status", "before": "in_review", "after": "done"}],
                },
            ]
        },
    )

    fact = provider.review_approval_fact("AID-7")

    assert fact is not None
    assert fact.activity_id == "change-2"
    assert fact.actor_type == "agent"
    assert fact.actor_id == "codex-agent"


def test_dashi_adapter_lists_review_comments() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> str:
        commands.append(tuple(command))
        return json.dumps(
            {
                "comments": [
                    {"id": "comment-1", "body": "请补测试。"},
                    {"id": "comment-2", "content": "请更新 README。"},
                ],
                "nextCursor": "2",
            }
        )

    comments = DashiTaskProvider(runner=runner, executable="taskctl").list_comments("AID-1")

    assert comments == ("请补测试。", "请更新 README。")
    assert commands == [("taskctl", "comment", "list", "AID-1", "--json")]


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
