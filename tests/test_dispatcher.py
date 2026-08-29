from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from workspace_orchestrator.adapters.agent import CodexExecutionResult
from workspace_orchestrator.automation.dispatcher import (
    AutoDispatcher,
    dispatcher_status,
    start_dispatcher,
    stop_dispatcher,
)
from workspace_orchestrator.models import Task
from workspace_orchestrator.workspace import WorkspaceStore


class FakeTasks:
    def __init__(self, task: Task) -> None:
        self.task = task
        self.comments: list[str] = ["请覆盖失败场景"]
        self.added_comments: list[str] = []

    def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
        return (self.task,)

    def list_comments(self, task_id: str) -> tuple[str, ...]:
        return tuple(self.comments)

    def get_task(self, task_id: str) -> Task:
        return self.task

    def update_status(self, task_id: str, status: str) -> Task:
        self.task = replace(self.task, status=status, version=(self.task.version or 0) + 1)
        return self.task

    def add_comment(self, task_id: str, body: str) -> None:
        self.added_comments.append(body)


class FakeExecutor:
    def __init__(self, tasks: FakeTasks, *, returncode: int = 0) -> None:
        self.tasks = tasks
        self.returncode = returncode
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        workspace_path: Path,
        prompt: str,
        *,
        sandbox: str,
        resume_session_id: str | None,
    ) -> CodexExecutionResult:
        self.calls.append(
            {
                "workspace_path": workspace_path,
                "prompt": prompt,
                "sandbox": sandbox,
                "resume_session_id": resume_session_id,
            }
        )
        if self.returncode == 0:
            self.tasks.update_status(self.tasks.task.id, "in_review")
        return CodexExecutionResult(
            returncode=self.returncode,
            session_id="thread-auto",
            stdout=(
                '{"type":"thread.started","thread_id":"thread-auto"}\n'
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"完成"}}\n'
            ),
            stderr="失败" if self.returncode else "",
        )


def _store(tmp_path: Path) -> tuple[WorkspaceStore, str]:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create(
        "自动执行",
        task_provider="dashi",
        task_project_id="demo",
    )
    store.touch_meta(requirement_id, status="in_progress")
    return store, requirement_id


def test_dispatcher_executes_unbound_in_progress_task_once(
    tmp_path: Path, monkeypatch
) -> None:
    store, requirement_id = _store(tmp_path)
    tasks = FakeTasks(
        Task(
            id="AID-1",
            raw_id="opaque-1",
            title="修复登录",
            description="修复错误密码处理",
            status="in_progress",
            labels=(f"requirement:{requirement_id}",),
            version=3,
        )
    )
    executor = FakeExecutor(tasks)
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher.configured_task_provider",
        lambda meta, root: tasks,
    )

    dispatcher = AutoDispatcher(store, executor)  # type: ignore[arg-type]
    assert dispatcher.run_once() == "completed"
    assert dispatcher.run_once() == "idle"

    call = executor.calls[0]
    assert call["workspace_path"] == tmp_path
    assert call["resume_session_id"] is None
    assert requirement_id in str(call["prompt"])
    assert "AID-1" in str(call["prompt"])
    assert "请覆盖失败场景" in str(call["prompt"])
    assert tasks.task.status == "in_review"


def test_dispatcher_resumes_latest_detached_task_session(
    tmp_path: Path, monkeypatch
) -> None:
    store, requirement_id = _store(tmp_path)
    sessions_path = store.path_for(requirement_id) / "sessions.json"
    store.write_json(
        sessions_path,
        [
            {
                "id": "thread-old",
                "agent": "codex",
                "started_at": "2026-08-29T00:00:00+08:00",
                "ended_at": "2026-08-29T00:10:00+08:00",
                "task_ids": ["AID-1"],
                "result": "detached",
            }
        ],
    )
    tasks = FakeTasks(
        Task(id="AID-1", title="返工", status="in_progress", version=8)
    )
    executor = FakeExecutor(tasks)
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher.configured_task_provider",
        lambda meta, root: tasks,
    )

    assert AutoDispatcher(store, executor).run_once() == "completed"  # type: ignore[arg-type]

    assert executor.calls[0]["resume_session_id"] == "thread-old"


def test_dispatcher_never_steals_bound_or_review_task(tmp_path: Path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    tasks = FakeTasks(
        Task(
            id="AID-1",
            title="已有会话",
            status="in_progress",
            binding_session_id="thread-active",
            version=2,
        )
    )
    executor = FakeExecutor(tasks)
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher.configured_task_provider",
        lambda meta, root: tasks,
    )

    assert AutoDispatcher(store, executor).run_once() == "idle"  # type: ignore[arg-type]
    assert executor.calls == []

    tasks.task = replace(
        tasks.task,
        binding_session_id=None,
        labels=("requirement-review",),
        version=3,
    )
    assert AutoDispatcher(store, executor).run_once() == "idle"  # type: ignore[arg-type]
    assert executor.calls == []


def test_dispatcher_blocks_missing_worktree_instead_of_looking_busy(
    tmp_path: Path, monkeypatch
) -> None:
    store, requirement_id = _store(tmp_path)
    tasks = FakeTasks(
        Task(
            id="AID-1",
            title="缺失工作树",
            status="in_progress",
            branch="feature/missing",
            worktree=".worktrees/missing",
            version=2,
        )
    )
    executor = FakeExecutor(tasks)
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher.configured_task_provider",
        lambda meta, root: tasks,
    )

    assert AutoDispatcher(store, executor).run_once() == "blocked"  # type: ignore[arg-type]

    assert tasks.task.status == "blocked"
    assert requirement_id in store.requirement_ids()
    assert "工作目录不存在" in tasks.added_comments[0]
    assert executor.calls == []


def test_dispatcher_background_process_starts_reports_status_and_stops(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path)
    started = start_dispatcher(store, explicit=True)
    try:
        assert started["running"] is True
        deadline = time.monotonic() + 5
        status = dispatcher_status(store)
        while status.get("status") == "starting" and time.monotonic() < deadline:
            time.sleep(0.05)
            status = dispatcher_status(store)
        assert status["running"] is True
        assert status["status"] == "running"
    finally:
        stopped = stop_dispatcher(store)
    assert stopped["running"] is False
    assert stopped["status"] == "stopped"
