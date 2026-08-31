from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from workspace_orchestrator.adapters.agent import CodexAgentProvider
from workspace_orchestrator.adapters.task import TaskProviderError
from workspace_orchestrator.automation.git_sync import collect_git_context
from workspace_orchestrator.automation.requirement_attach import (
    AutomationAmbiguity,
    discover_project_root,
)
from workspace_orchestrator.automation.runtime import AutomationRuntime
from workspace_orchestrator.automation.session_runtime import attach_session, end_session
from workspace_orchestrator.automation.task_attach import configured_task_provider
from workspace_orchestrator.cli import main
from workspace_orchestrator.models import ReviewApprovalFact, Task
from workspace_orchestrator.project_init import initialize_project
from workspace_orchestrator.review_packet import build_review_packet, render_review_packet
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _init_git(path: Path) -> None:
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("Example: hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _add_remote_and_push(path: Path) -> Path:
    remote = path.parent / f"{path.name}-remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(path, "remote", "add", "origin", str(remote))
    _git(path, "push", "-u", "origin", "HEAD")
    return remote


class FakeTasks:
    def __init__(self) -> None:
        self.tasks: dict[str, list[Task]] = {}
        self.created: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.unlinks: list[tuple[str, str]] = []
        self.git_updates: list[tuple[str, str | None, str | None]] = []
        self.comments: dict[str, list[str]] = {}
        self.review_publications: list[str] = []
        self.approval_actors: dict[str, str | None] = {}

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

    def publish_review(self, task_id: str, content: str) -> Task:
        requirement_id, index, task = self._find(task_id)
        if task.description == content:
            return task
        updated = replace(task, description=content)
        self.tasks[requirement_id][index] = updated
        self.review_publications.append(task_id)
        return updated

    def review_approval_fact(self, task_id: str) -> ReviewApprovalFact | None:
        actor_type = self.approval_actors.get(task_id, "user")
        if actor_type is None:
            return None
        return ReviewApprovalFact(
            activity_id=f"activity-{task_id}",
            actor_type=actor_type,
            actor_id="local-user" if actor_type == "user" else "codex-agent",
            actor_name="本地用户" if actor_type == "user" else "Codex Agent",
            changed_at="2026-08-29T08:00:00Z",
        )

    def add_comment(self, task_id: str, body: str) -> None:
        self.comments.setdefault(task_id, []).append(body)

    def list_comments(self, task_id: str) -> tuple[str, ...]:
        return tuple(self.comments.get(task_id, ()))

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


def _seed_current_review_packet(
    store: WorkspaceStore, requirement_id: str, tasks: FakeTasks, *, revision: int = 1
) -> None:
    git = collect_git_context(store.project_root, {}, execution_root=store.working_root)
    packet = build_review_packet(
        store, requirement_id, tasks=tasks.list_tasks(requirement_id), git=git
    )
    req, index, review_task = tasks._find("TASK-REVIEW")
    tasks.tasks[req][index] = replace(
        review_task, description=render_review_packet(packet, revision)
    )
    store.touch_meta(
        requirement_id,
        requirement_review_task_id="TASK-REVIEW",
        review_packet_revision=revision,
        review_packet_fingerprint=packet.fingerprint,
        review_packet_published_revision=revision,
        review_packet_published_fingerprint=packet.fingerprint,
    )


def _reviewable_runtime(
    tmp_path: Path, tasks: FakeTasks | None = None
) -> tuple[WorkspaceStore, str, FakeTasks, AutomationRuntime]:
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
    requirement_id = store.create("Review Packet", manual_test_required=True)
    provider = tasks or FakeTasks()
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "packet-thread"}),
        provider,
    )
    runtime.bootstrap(requirement_id, development_request="实现审查材料")
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "requirement.md",
        data["requirement"].replace("- [ ] 定义验收标准", "- [x] 定义验收标准"),
    )
    store.write_text(data["path"] / "intent.md", data["intent"].replace("：PARTIAL", "：PASS"))
    return store, requirement_id, provider, runtime


def test_black_box_a_and_b_bootstrap_then_repeat_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """场景 A/B：薄触发器只调用 bootstrap，后续链路全由 Runtime 完成。"""

    _init_git(tmp_path)
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("唯一活动需求", task_provider="dashi", task_project_id="demo")
    tasks = FakeTasks()
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    monkeypatch.setattr(
        "workspace_orchestrator.automation.task_attach.DashiTaskProvider",
        lambda **_: tasks,
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


def test_default_dashi_session_start_creates_visible_work_card_then_reuses_it(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Deferred task creation")
    tasks = FakeTasks()
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-start"}),
        tasks,
    )

    runtime.bootstrap(requirement_id)
    assert tasks.created == ["TASK-001"]
    assert tasks.get_task("TASK-001").status == "todo"
    assert store.attached_requirement_id("thread-start") == requirement_id

    runtime.bootstrap(requirement_id, development_request="实现首个用户请求")
    assert tasks.created == ["TASK-001"]
    assert tasks.get_task("TASK-001").status == "in_progress"
    assert tasks.links == [("TASK-001", "thread-start")]


def test_configured_dashi_provider_automatically_ensures_taskboard_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("自动启动任务面板", task_provider="dashi", task_project_id="demo")
    captured: dict[str, object] = {}
    tasks = FakeTasks()

    def factory(*, project_id: str, service_starter: object, **_: object) -> FakeTasks:
        captured.update(project_id=project_id, service_starter=service_starter)
        return tasks

    monkeypatch.setattr("workspace_orchestrator.automation.task_attach.DashiTaskProvider", factory)

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


def test_explicit_new_requirement_request_creates_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    store.create("First")
    store.create("Second")
    tasks = FakeTasks()
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-new"}),
        tasks,
    )
    prompt = "新增需求：参考 REQ-001 修复任务面板"

    first = runtime.bootstrap(development_request=prompt, creation_key="turn-1")
    second = runtime.bootstrap(development_request=prompt, creation_key="turn-1")

    assert store.requirement_ids() == ("REQ-001", "REQ-002", "REQ-003")
    created = store.load("REQ-003")
    assert created["meta"]["creation_key"] == "turn-1"
    assert created["meta"]["manual_test_required"] is False
    assert "参考 REQ-001 修复任务面板" in created["requirement"]
    assert store.attached_requirement_id("thread-new") == "REQ-003"
    assert len(tasks.list_tasks("REQ-003")) == 1
    assert "REQ-003" in first and "REQ-003" in second


def test_manual_test_is_only_enabled_by_positive_request(tmp_path: Path) -> None:
    tasks = FakeTasks()
    store = WorkspaceStore(tmp_path)
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-manual"}),
        tasks,
    )

    runtime.bootstrap(
        development_request="新增需求：默认自动完成，除非明确要求人工测试",
        creation_key="turn-default",
    )
    end_session(store, "REQ-001", "thread-manual", task_provider=tasks)
    runtime.bootstrap(
        development_request="新增需求：这次需要人工测试后再完成",
        creation_key="turn-manual",
    )

    assert store.load("REQ-001")["meta"]["manual_test_required"] is False
    assert store.load("REQ-002")["meta"]["manual_test_required"] is True

    end_session(store, "REQ-002", "thread-manual", task_provider=tasks)
    runtime.bootstrap(
        development_request="新增需求：不需要人工测试，验证通过直接结束",
        creation_key="turn-negative",
    )
    assert store.load("REQ-003")["meta"]["manual_test_required"] is False


def test_taskboard_visibility_backfills_active_requirements_once(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("Only completed task")
    second = store.create("No task")
    done = store.create("Completed requirement")
    store.touch_meta(done, status="done")
    tasks = FakeTasks()
    tasks.tasks[first] = [Task(id="TASK-OLD", title="Old", status="done")]
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-board"}),
        tasks,
    )

    runtime.sync_taskboard_visibility()
    runtime.sync_taskboard_visibility()

    first_visible = [task for task in tasks.list_tasks(first) if task.status == "todo"]
    second_visible = [task for task in tasks.list_tasks(second) if task.status == "todo"]
    assert len(first_visible) == 2
    assert len(second_visible) == 2
    assert sum("requirement-work" in task.labels for task in first_visible) == 1
    assert sum("requirement-space" in task.labels for task in first_visible) == 1
    done_spaces = tasks.list_tasks(done)
    assert len(done_spaces) == 1
    assert "requirement-space" in done_spaces[0].labels

    tasks.update_status(first_visible[0].id, "done")
    runtime.sync_taskboard_visibility()
    assert len([task for task in tasks.list_tasks(first) if task.status == "todo"]) == 1


def test_requirement_space_can_be_closed_without_deleting_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("可回看需求")
    tasks = FakeTasks()
    runtime = AutomationRuntime(store, CodexAgentProvider(environ={}), tasks)

    runtime.sync_taskboard_visibility()
    space = next(
        task for task in tasks.list_tasks(requirement_id) if "requirement-space" in task.labels
    )
    tasks.update_status(space.id, "done")
    runtime.sync_taskboard_visibility()

    assert store.path_for(requirement_id).is_dir()
    assert store.load(requirement_id)["meta"]["requirement_space_closed"] is True
    assert sum("requirement-space" in task.labels for task in tasks.list_tasks(requirement_id)) == 1


def test_parallel_sessions_require_distinct_tasks_branches_and_worktrees(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("并行任务", task_provider=None)

    attach_session(
        store,
        requirement_id,
        session_id="thread-one",
        agent_name="codex",
        task_ids=("TASK-ONE",),
        branch="codex/one",
        worktree=str(tmp_path / "one"),
    )
    attach_session(
        store,
        requirement_id,
        session_id="thread-two",
        agent_name="codex",
        task_ids=("TASK-TWO",),
        branch="codex/two",
        worktree=str(tmp_path / "two"),
    )

    with pytest.raises(WorkspaceError, match="同一 Requirement 的并行 Agent"):
        attach_session(
            store,
            requirement_id,
            session_id="thread-three",
            agent_name="codex",
            task_ids=("TASK-THREE",),
        )
    with pytest.raises(WorkspaceError, match="Task TASK-ONE"):
        attach_session(
            store,
            requirement_id,
            session_id="thread-four",
            agent_name="codex",
            task_ids=("TASK-ONE",),
            branch="codex/four",
            worktree=str(tmp_path / "four"),
        )


def test_taskboard_backfill_runs_before_multiple_requirement_ambiguity(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("First hidden")
    second = store.create("Second hidden")
    tasks = FakeTasks()
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-ambiguous-board"}),
        tasks,
    )

    with pytest.raises(AutomationAmbiguity, match="ambiguity"):
        runtime.bootstrap(development_request="普通修改")

    assert len(tasks.list_tasks(first)) == 2
    assert len(tasks.list_tasks(second)) == 2


def test_finalize_runs_known_verification_and_auto_completes_by_default(
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
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "requirement.md",
        data["requirement"].replace("- [ ] 定义验收标准", "- [x] 定义验收标准"),
    )
    store.write_text(data["path"] / "intent.md", data["intent"].replace("：PARTIAL", "：PASS"))

    result = runtime.finalize(requirement_id, completed=("README 中文示例",))

    data = store.load(requirement_id)
    assert result.passed is True
    assert result.requirement_completed is True
    assert tasks.get_task("TASK-001").status == "done"
    assert tasks.unlinks == [("TASK-001", "thread-final")]
    assert data["sessions"][0]["result"] == "completed"
    assert "- README.md" in data["handoff"]
    assert "状态：PASS" in data["verification"]
    assert "状态：TODO" not in data["verification"]
    assert data["meta"]["status"] == "done"
    assert data["meta"]["completion_mode"] == "auto_after_verification"
    review_tasks = [
        task for task in tasks.list_tasks(requirement_id) if "requirement-review" in task.labels
    ]
    assert review_tasks == []
    assert result.review_task_id is None


def test_concurrent_finalize_never_rolls_done_back_to_in_progress(tmp_path: Path) -> None:
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)
    store.touch_meta(requirement_id, manual_test_required=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.finalize(requirement_id), range(2)))

    assert sum(result.passed for result in results) == 1
    assert store.load(requirement_id)["meta"]["status"] == "done"
    assert tasks.get_task("TASK-001").status == "done"


def test_finalize_publishes_complete_idempotent_review_packet(tmp_path: Path) -> None:
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)

    result = runtime.finalize(requirement_id, completed=("确定性 Review Packet",))

    assert result.passed is True
    review_task = tasks.get_task(str(result.review_task_id))
    for heading in (
        "需求目标与范围",
        "本次完成内容",
        "验收标准及状态",
        "验证命令与结果",
        "修改文件",
        "Git 上下文",
        "已知问题与风险",
        "关联开发 Task",
        "批准或退回",
    ):
        assert heading in review_task.description
    assert "ai-dev-os-review-packet:v1" in review_task.description
    assert "结果：pytest" in review_task.description
    assert "结果：ruff" in review_task.description
    assert "结果：integration" in review_task.description
    assert "结果：状态：" not in review_task.description
    publications = len(tasks.review_publications)
    fingerprint = store.load(requirement_id)["meta"]["review_packet_fingerprint"]
    blockers, _ = runtime._publish_review_packet(requirement_id, tasks)
    assert blockers == ()
    assert len(tasks.review_publications) == publications
    assert store.load(requirement_id)["meta"]["review_packet_fingerprint"] == fingerprint


def test_review_packet_evidence_change_updates_revision(tmp_path: Path) -> None:
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)
    assert runtime.finalize(requirement_id, completed=("第一版",)).passed
    first = store.load(requirement_id)["meta"]
    store.touch_meta(requirement_id, status="in_progress")
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "state.md",
        data["state"].replace("- 第一版", "- 第一版\n- 第二版证据"),
    )

    blockers, _ = runtime._publish_review_packet(requirement_id, tasks)

    second = store.load(requirement_id)["meta"]
    assert blockers == ()
    assert second["review_packet_revision"] == first["review_packet_revision"] + 1
    assert second["review_packet_fingerprint"] != first["review_packet_fingerprint"]
    assert "第二版证据" in tasks.get_task(second["requirement_review_task_id"]).description


def test_review_packet_publish_failure_blocks_review_ready(tmp_path: Path) -> None:
    class OfflinePublishTasks(FakeTasks):
        def publish_review(self, task_id: str, content: str) -> Task:
            raise TaskProviderError("正文更新失败")

    store, requirement_id, _, runtime = _reviewable_runtime(tmp_path, OfflinePublishTasks())

    result = runtime.finalize(requirement_id, completed=("实现完成",))

    assert result.passed is False
    assert "Review Packet 发布失败" in result.blockers[0]
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"
    assert store.attached_requirement_id("packet-thread") == requirement_id


def test_finalize_moves_all_related_active_development_tasks_to_review(tmp_path: Path) -> None:
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)
    tasks.tasks[requirement_id].append(
        Task(id="TASK-UNBOUND", title="同需求未绑定任务", status="in_progress")
    )

    result = runtime.finalize(requirement_id, completed=("全部开发任务",))

    assert result.passed is True
    assert tasks.get_task("TASK-UNBOUND").status == "in_review"
    assert store.load(requirement_id)["meta"]["status"] == "in_review"


def test_done_review_card_with_stale_revision_cannot_approve(tmp_path: Path) -> None:
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)
    result = runtime.finalize(requirement_id, completed=("第一版",))
    review_id = str(result.review_task_id)
    tasks.update_status(review_id, "done")
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "state.md",
        data["state"].replace("- 第一版", "- 第一版\n- 审批前新增证据"),
    )

    messages = runtime.sync_reviews(requirement_id)

    assert "旧 revision 不会批准" in messages[0]
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"


def test_concurrent_review_approval_and_return_are_idempotent(tmp_path: Path) -> None:
    store, requirement_id, tasks, runtime = _reviewable_runtime(tmp_path)
    result = runtime.finalize(requirement_id, completed=("并发审查",))
    review_id = str(result.review_task_id)
    tasks.update_status(review_id, "done")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: runtime.sync_reviews(requirement_id), range(2)))
    assert store.load(requirement_id)["meta"]["status"] == "done"
    assert any(item for outcome in outcomes for item in outcome if "批准" in item)

    # 新一轮审查只用于验证相同退回事件不会重复记录。
    store.touch_meta(requirement_id, status="in_review")
    tasks.update_status(review_id, "in_progress")
    tasks.comments[review_id] = ["请补充并发说明。"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: runtime.sync_reviews(requirement_id), range(2)))
    data = store.load(requirement_id)
    assert data["meta"]["status"] == "in_progress"
    assert data["state"].count("请补充并发说明。") == 3


def test_end_session_provider_io_does_not_hold_workspace_lock(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingTasks(FakeTasks):
        def unlink_session(self, task_id: str, session_id: str) -> None:
            started.set()
            assert release.wait(10)
            super().unlink_session(task_id, session_id)

    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("SessionEnd lock")
    tasks = BlockingTasks()
    tasks.tasks[requirement_id] = [Task(id="TASK-001", title="开发", status="in_progress")]
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "end-thread"}),
        tasks,
    )
    runtime.bootstrap(requirement_id, task_ids=("TASK-001",))
    worker = threading.Thread(
        target=end_session,
        args=(store, requirement_id, "end-thread"),
        kwargs={"task_provider": tasks},
    )
    worker.start()
    assert started.wait(5)
    before = time.monotonic()
    store.touch_meta(requirement_id, concurrent_write=True)
    assert time.monotonic() - before < 1
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert store.load(requirement_id)["meta"]["concurrent_write"] is True


def test_dashi_review_task_done_is_explicit_requirement_approval(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Review approval")
    store.touch_meta(requirement_id, status="in_review")
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(id="TASK-001", title="开发任务", status="done"),
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="done",
            labels=("requirement-review",),
        ),
    ]
    _seed_current_review_packet(store, requirement_id, tasks)
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "review-sync"}),
        tasks,
    )

    messages = runtime.sync_reviews(requirement_id)

    assert store.load(requirement_id)["meta"]["status"] == "done"
    assert store.load(requirement_id)["meta"]["review_confirmation_source"] == "TASK-REVIEW"
    assert "用户已在 dashi 批准" in messages[0]
    assert tasks.get_task("TASK-001").status == "done"


@pytest.mark.parametrize("actor_type", ["agent", "script", None])
def test_done_review_card_without_reliable_user_actor_never_approves(
    tmp_path: Path, actor_type: str | None
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Review actor")
    store.touch_meta(requirement_id, status="in_review")
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="done",
            labels=("requirement-review",),
        )
    ]
    tasks.approval_actors["TASK-REVIEW"] = actor_type
    _seed_current_review_packet(store, requirement_id, tasks)

    messages = AutomationRuntime(store, CodexAgentProvider(environ={}), tasks).sync_reviews(
        requirement_id
    )

    assert store.load(requirement_id)["meta"]["status"] == "in_review"
    assert "Requirement" in messages[0]
    assert "批准" in messages[0] or "操作者" in messages[0]


def test_cli_confirm_rejects_missing_and_stale_dashi_review_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = WorkspaceStore(tmp_path)
    missing_id = store.create("Missing packet")
    store.touch_meta(missing_id, status="in_review")
    tasks = FakeTasks()
    monkeypatch.setattr(
        "workspace_orchestrator.automation.runtime.configured_task_provider",
        lambda *_: tasks,
    )

    assert main(["--root", str(tmp_path), "confirm", missing_id, "--user-confirmed"]) == 2
    assert "Review Packet" in capsys.readouterr().err
    assert store.load(missing_id)["meta"]["status"] == "in_review"

    store.touch_meta(missing_id, status="done")
    stale_id = store.create("Stale packet")
    store.touch_meta(stale_id, status="in_review")
    tasks.tasks[stale_id] = [
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="in_review",
            labels=("requirement-review",),
        )
    ]
    _seed_current_review_packet(store, stale_id, tasks)
    data = store.load(stale_id)
    store.write_text(
        data["path"] / "state.md",
        data["state"].replace("## 已完成\n\n无", "## 已完成\n\n- 新证据"),
    )

    assert main(["--root", str(tmp_path), "confirm", stale_id, "--user-confirmed"]) == 2
    assert "已陈旧" in capsys.readouterr().err
    assert store.load(stale_id)["meta"]["status"] == "in_review"


def test_workspace_status_syncs_dashi_review_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Status approval")
    store.touch_meta(
        requirement_id,
        status="in_review",
        requirement_review_task_id="TASK-REVIEW",
        review_comment_count=0,
    )
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="done",
            labels=("requirement-review",),
        )
    ]
    _seed_current_review_packet(store, requirement_id, tasks)
    monkeypatch.setattr(
        "workspace_orchestrator.automation.runtime.configured_task_provider",
        lambda *_: tasks,
    )

    assert main(["--root", str(tmp_path), "status", requirement_id]) == 0

    assert "done（已完成）" in capsys.readouterr().out
    assert store.load(requirement_id)["meta"]["status"] == "done"


def test_ordinary_done_task_never_approves_requirement(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("No inferred approval")
    store.touch_meta(requirement_id, status="in_review")
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(id="TASK-001", title="开发任务", status="done"),
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="in_review",
            labels=("requirement-review",),
        ),
    ]
    _seed_current_review_packet(store, requirement_id, tasks)
    runtime = AutomationRuntime(store, CodexAgentProvider(environ={}), tasks)

    assert runtime.sync_reviews(requirement_id) == ()
    assert store.load(requirement_id)["meta"]["status"] == "in_review"


def test_review_task_identity_ignores_other_tasks_with_same_label(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Stable review identity")
    store.touch_meta(
        requirement_id,
        status="in_review",
        requirement_review_task_id="TASK-REVIEW",
        review_comment_count=0,
    )
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(
            id="TASK-WRONG",
            title="普通任务误带标签",
            status="done",
            labels=("requirement-review",),
        ),
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="in_review",
            labels=("requirement-review",),
        ),
    ]
    _seed_current_review_packet(store, requirement_id, tasks)

    AutomationRuntime(store, CodexAgentProvider(environ={}), tasks).sync_reviews(requirement_id)

    assert store.load(requirement_id)["meta"]["status"] == "in_review"


def test_dashi_review_task_return_records_comment_and_reopens_development_tasks(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Review changes")
    store.touch_meta(requirement_id, status="in_review")
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(id="TASK-001", title="开发任务", status="in_review"),
        Task(id="TASK-002", title="已完成任务", status="done"),
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="in_progress",
            labels=("requirement-review",),
        ),
    ]
    _seed_current_review_packet(store, requirement_id, tasks)
    tasks.comments["TASK-REVIEW"] = ["请补充离线重试说明。"]
    runtime = AutomationRuntime(store, CodexAgentProvider(environ={}), tasks)

    runtime.sync_reviews(requirement_id)

    data = store.load(requirement_id)
    assert data["meta"]["status"] == "in_progress"
    assert "请补充离线重试说明。" in data["state"]
    assert tasks.get_task("TASK-001").status == "in_progress"
    assert tasks.get_task("TASK-002").status == "done"


def test_dashi_review_task_in_progress_without_new_comment_does_not_reopen(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Review needs comment")
    store.touch_meta(
        requirement_id,
        status="in_review",
        requirement_review_task_id="TASK-REVIEW",
        review_comment_count=1,
    )
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="in_progress",
            labels=("requirement-review",),
        )
    ]
    _seed_current_review_packet(store, requirement_id, tasks)
    tasks.comments["TASK-REVIEW"] = ["上一轮旧留言"]

    messages = AutomationRuntime(store, CodexAgentProvider(environ={}), tasks).sync_reviews(
        requirement_id
    )

    assert "没有本轮新留言" in messages[0]
    assert store.load(requirement_id)["meta"]["status"] == "in_review"


def test_missing_review_packet_blocks_review_ready_and_restores_retryable_state(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Offline review card")
    store.touch_meta(requirement_id, status="in_review")

    class OfflineCreateTasks(FakeTasks):
        def create_task(self, requirement_id: str, task: Task) -> Task:
            raise TaskProviderError("offline")

    runtime = AutomationRuntime(store, CodexAgentProvider(environ={}), OfflineCreateTasks())

    assert "Review Packet" in runtime.sync_reviews(requirement_id)[0]
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"


def test_repeated_finalize_never_resets_user_approved_review_task(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Already approved")
    store.touch_meta(
        requirement_id,
        status="in_review",
        requirement_review_task_id="TASK-REVIEW",
        review_comment_count=0,
    )
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="done",
            labels=("requirement-review",),
        )
    ]
    _seed_current_review_packet(store, requirement_id, tasks)
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "repeat-finalize"}),
        tasks,
    )

    result = runtime.finalize(requirement_id)

    assert result.passed is False
    assert store.load(requirement_id)["meta"]["status"] == "done"
    assert tasks.get_task("TASK-REVIEW").status == "done"


def test_bootstrap_syncs_review_approval_without_attaching_done_requirement(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Approved before bootstrap")
    store.touch_meta(
        requirement_id,
        status="in_review",
        requirement_review_task_id="TASK-REVIEW",
        review_comment_count=0,
    )
    tasks = FakeTasks()
    tasks.tasks[requirement_id] = [
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="done",
            labels=("requirement-review",),
        )
    ]
    _seed_current_review_packet(store, requirement_id, tasks)
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "approval-bootstrap"}),
        tasks,
    )

    snapshot = runtime.bootstrap(requirement_id)

    assert "Requirement 已进入 done" in snapshot
    assert store.load(requirement_id)["sessions"] == []


def test_request_changes_offline_is_retried_until_tasks_converge(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Offline review changes")
    store.touch_meta(requirement_id, status="in_review")

    class FlakyReviewTasks(FakeTasks):
        offline = True

        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            if self.offline:
                raise TaskProviderError("offline")
            return super().list_tasks(requirement_id)

    tasks = FlakyReviewTasks()
    tasks.tasks[requirement_id] = [
        Task(id="TASK-001", title="开发任务", status="in_review"),
        Task(
            id="TASK-REVIEW",
            title="Requirement Review",
            status="in_review",
            labels=("requirement-review",),
        ),
    ]
    runtime = AutomationRuntime(store, CodexAgentProvider(environ={}), tasks)

    runtime.request_changes(requirement_id, feedback="请修改错误处理。")
    assert store.load(requirement_id)["meta"]["pending_task_review_reopen"] is True

    tasks.offline = False
    runtime.sync_reviews(requirement_id)
    assert store.load(requirement_id)["meta"]["pending_task_review_reopen"] is False
    assert {task.status for task in tasks.list_tasks(requirement_id)} == {"in_progress"}


def test_bootstrap_degrades_when_provider_is_offline_then_retries_and_converges(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Offline provider")

    class FlakyTasks(FakeTasks):
        def __init__(self) -> None:
            super().__init__()
            self.offline = True
            self.fail_link_once = True

        def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
            if self.offline:
                raise TaskProviderError("offline")
            return super().list_tasks(requirement_id)

        def link_session(self, task_id: str, session_id: str, **_: object) -> None:
            if self.fail_link_once:
                self.fail_link_once = False
                raise TaskProviderError("link offline")
            super().link_session(task_id, session_id)

    tasks = FlakyTasks()
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-retry"}),
        tasks,
    )

    first = runtime.bootstrap(requirement_id, development_request="继续本地开发")
    assert "不可用（offline）" in first
    assert store.attached_requirement_id("thread-retry") == requirement_id

    tasks.offline = False
    runtime.bootstrap(requirement_id, development_request="继续本地开发")
    assert store.load(requirement_id)["sessions"][0]["pending_link_task_ids"] == ["TASK-001"]

    runtime.bootstrap(requirement_id, development_request="继续本地开发")
    session = store.load(requirement_id)["sessions"][0]
    assert "pending_link_task_ids" not in session
    assert tasks.links == [("TASK-001", "thread-retry")]


def test_offline_session_unlink_is_retried_by_later_bootstrap(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Retry unlink")

    class FlakyUnlinkTasks(FakeTasks):
        fail_unlink_once = True

        def unlink_session(self, task_id: str, session_id: str) -> None:
            if self.fail_unlink_once:
                self.fail_unlink_once = False
                raise TaskProviderError("unlink offline")
            super().unlink_session(task_id, session_id)

    tasks = FlakyUnlinkTasks()
    first = AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-old"}),
        tasks,
    )
    first.bootstrap(requirement_id, development_request="实现功能")
    first.handoff(requirement_id)
    assert store.load(requirement_id)["sessions"][0]["pending_unlink_task_ids"] == ["TASK-001"]

    AutomationRuntime(
        store,
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-new"}),
        tasks,
    ).bootstrap(requirement_id)

    old_session = store.load(requirement_id)["sessions"][0]
    assert "pending_unlink_task_ids" not in old_session
    assert tasks.unlinks == [("TASK-001", "thread-old")]


def test_finalize_returns_review_blockers_and_keeps_session_retryable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.workspace-orchestrator.automation]\n"
        'verification-commands = [["{python}", "-c", "print(1)"]]\n',
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Blocked finalize")
    runtime = AutomationRuntime(
        store, CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-blocked"})
    )
    runtime.bootstrap(requirement_id)

    result = runtime.finalize(requirement_id)

    assert result.passed is False
    assert any("意图一致性" in item for item in result.blockers)
    assert any("验收标准" in item for item in result.blockers)
    assert store.attached_requirement_id("thread-blocked") == requirement_id
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"
    assert "审查阻塞项" in store.load(requirement_id)["handoff"]


def test_verification_timeout_is_reported_as_finalize_blocker(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.workspace-orchestrator.automation]\n"
        "verification-timeout-seconds = 0.05\n"
        'verification-commands = [["{python}", "-c", "import time; time.sleep(1)"]]\n',
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Timeout")
    runtime = AutomationRuntime(
        store, CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-timeout"})
    )
    runtime.bootstrap(requirement_id)

    result = runtime.finalize(requirement_id)

    assert result.passed is False
    assert "超时" in result.verification
    assert "超时" in result.blockers[0]


def test_invalid_verification_config_is_reported_as_finalize_blocker(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.workspace-orchestrator.automation]\nverification-commands = ["pytest"]\n',
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Invalid config")
    runtime = AutomationRuntime(
        store, CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-config"})
    )
    runtime.bootstrap(requirement_id)

    result = runtime.finalize(requirement_id)

    assert result.passed is False
    assert "验证配置无效" in result.blockers[0]


def test_project_discovery_walks_up_to_workspace(tmp_path: Path) -> None:
    WorkspaceStore(tmp_path).create("Discover")
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)

    assert discover_project_root(nested) == tmp_path


def test_project_discovery_uses_main_workspace_from_linked_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "linked"
    main.mkdir()
    _init_git(main)
    WorkspaceStore(main).create("Shared requirement")
    _git(main, "worktree", "add", "--detach", str(worktree))

    assert discover_project_root(worktree) == main


def test_shared_workspace_keeps_git_execution_in_linked_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "linked"
    main.mkdir()
    _init_git(main)
    requirement_id = WorkspaceStore(main).create("Worktree execution", task_provider=None)
    _git(main, "worktree", "add", "--detach", str(worktree))
    (worktree / "README.md").write_text("worktree change\n", encoding="utf-8")
    store = WorkspaceStore(main, execution_root=worktree)

    snapshot = AutomationRuntime(
        store, CodexAgentProvider(environ={"CODEX_THREAD_ID": "worktree-thread"})
    ).bootstrap(requirement_id)

    assert "README.md" in snapshot
    assert store.load(requirement_id)["meta"]["git"]["worktree"] == str(worktree)


def test_two_requirements_in_prebound_worktrees_keep_sessions_and_git_state_isolated(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    first_worktree = tmp_path / "first"
    second_worktree = tmp_path / "second"
    main.mkdir()
    _init_git(main)
    store = WorkspaceStore(main)
    first = store.create("First concurrent requirement", task_provider=None)
    second = store.create("Second concurrent requirement", task_provider=None)
    _git(main, "worktree", "add", "--detach", str(first_worktree))
    _git(main, "worktree", "add", "--detach", str(second_worktree))

    AutomationRuntime(
        WorkspaceStore(main, execution_root=first_worktree),
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-first"}),
    ).bootstrap(first)
    AutomationRuntime(
        WorkspaceStore(main, execution_root=second_worktree),
        CodexAgentProvider(environ={"CODEX_THREAD_ID": "thread-second"}),
    ).bootstrap(second)

    first_data = store.load(first)
    second_data = store.load(second)
    assert first_data["sessions"][0]["id"] == "thread-first"
    assert second_data["sessions"][0]["id"] == "thread-second"
    assert first_data["meta"]["git"]["worktree"] == str(first_worktree)
    assert second_data["meta"]["git"]["worktree"] == str(second_worktree)


def test_legacy_multi_requirement_session_is_rejected_without_mutation(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.create("First")
    second = store.create("Second")
    store.write_json(
        store.path_for(first) / "sessions.json",
        [{"id": "shared-thread", "result": "in_progress", "task_ids": []}],
    )
    store.write_json(
        store.path_for(second) / "sessions.json",
        [{"id": "shared-thread", "result": "in_progress", "task_ids": []}],
    )
    runtime = AutomationRuntime(
        store, CodexAgentProvider(environ={"CODEX_THREAD_ID": "shared-thread"})
    )

    with pytest.raises(WorkspaceError, match="同时关联了多个需求"):
        runtime.bootstrap(first)

    assert store.active_session_conflicts() == {"shared-thread": (first, second)}


def test_stop_auto_finishes_only_after_thread_commit_is_clean_and_pushed(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    initialize_project(tmp_path)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initialize ai dev os")
    _add_remote_and_push(tmp_path)
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Auto finish")
    tasks = FakeTasks()
    archived: list[str] = []
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(
            environ={"CODEX_THREAD_ID": "thread-pushed"},
            archive_runner=archived.append,
        ),
        tasks,
    )
    runtime.bootstrap(requirement_id, development_request="修改 README")

    unchanged = runtime.auto_finish_pushed_thread()
    assert unchanged.completed is False
    assert "没有产生新提交" in unchanged.reason

    (tmp_path / "README.md").write_text("updated\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "update readme")
    unpushed = runtime.auto_finish_pushed_thread()
    assert unpushed.completed is False
    assert "上游完全同步" in unpushed.reason
    assert tasks.get_task("TASK-001").status == "in_progress"

    _git(tmp_path, "push")
    completed = runtime.auto_finish_pushed_thread()

    assert completed.completed is True
    assert completed.task_ids == ("TASK-001",)
    assert tasks.get_task("TASK-001").status == "done"
    assert archived == ["thread-pushed"]
    assert tasks.unlinks == [("TASK-001", "thread-pushed")]
    assert store.load(requirement_id)["sessions"][0]["result"] == "completed"
    assert store.load(requirement_id)["meta"]["status"] != "done"


def test_finalize_then_push_keeps_pending_record_until_stop_archives(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    initialize_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.workspace-orchestrator.automation]\n"
        "verification-commands = [\n"
        '  ["{python}", "-c", "print(\'pytest\')"],\n'
        '  ["{python}", "-c", "print(\'ruff\')"],\n'
        '  ["{python}", "-c", "print(\'integration\')"],\n'
        "]\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initialize")
    _add_remote_and_push(tmp_path)
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Finalize before push")
    tasks = FakeTasks()
    archived: list[str] = []
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(
            environ={"CODEX_THREAD_ID": "thread-finalize-push"},
            archive_runner=archived.append,
        ),
        tasks,
    )
    runtime.bootstrap(requirement_id, development_request="修改 README")
    data = store.load(requirement_id)
    store.write_text(
        data["path"] / "requirement.md",
        data["requirement"].replace("- [ ] 定义验收标准", "- [x] 定义验收标准"),
    )
    store.write_text(data["path"] / "intent.md", data["intent"].replace("：PARTIAL", "：PASS"))

    finalized = runtime.finalize(requirement_id)

    assert finalized.passed and finalized.requirement_completed
    assert store.load(requirement_id)["sessions"][0]["result"] == "pending_auto_finish"
    resumed = runtime.bootstrap(development_request="提交并推送当前修改")
    assert requirement_id in resumed
    assert store.load(requirement_id)["sessions"][0]["result"] == "pending_auto_finish"
    with pytest.raises(WorkspaceError, match="等待.*自动归档"):
        runtime.bootstrap(
            development_request="新增需求：不应在待归档 Thread 中创建",
            creation_key="blocked-new",
        )
    assert store.requirement_ids() == (requirement_id,)
    assert runtime.auto_finish_pushed_thread().completed is False
    assert store.load(requirement_id)["sessions"][0]["result"] == "pending_auto_finish"

    (tmp_path / "README.md").write_text("finalized and pushed\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "finish requirement")
    _git(tmp_path, "push")

    completed = runtime.auto_finish_pushed_thread()
    assert completed.completed is True
    assert archived == ["thread-finalize-push"]
    assert store.load(requirement_id)["sessions"][0]["result"] == "completed"
    assert store.load(requirement_id)["meta"]["status"] == "done"


def test_stop_auto_finish_can_be_disabled(tmp_path: Path) -> None:
    _init_git(tmp_path)
    initialize_project(tmp_path)
    config_path = tmp_path / ".ai-dev-os.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["automation"]["auto_finish_pushed_thread"] = False
    config_path.write_text(json.dumps(config), encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Disabled auto finish")
    tasks = FakeTasks()
    archived: list[str] = []
    runtime = AutomationRuntime(
        store,
        CodexAgentProvider(
            environ={"CODEX_THREAD_ID": "thread-disabled"},
            archive_runner=archived.append,
        ),
        tasks,
    )
    runtime.bootstrap(requirement_id, development_request="修改 README")

    result = runtime.auto_finish_pushed_thread()

    assert result.completed is False
    assert "已关闭" in result.reason
    assert tasks.get_task("TASK-001").status == "in_progress"
    assert archived == []


def test_repo_hook_config_and_script_drive_session_lifecycle(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    hook_config = json.loads((repo_root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert set(hook_config["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
    }
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
