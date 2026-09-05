from __future__ import annotations

import json
import signal
import time
from dataclasses import replace
from pathlib import Path

import pytest

from workspace_orchestrator.adapters.agent import CodexExecutionResult
from workspace_orchestrator.adapters.base import TaskProviderError
from workspace_orchestrator.automation.delegation import (
    decide_delegation,
    delegate_task,
    request_cancel,
    worker_status,
)
from workspace_orchestrator.automation.dispatcher import (
    AutoDispatcher,
    _only_managed_hooks,
    dispatcher_status,
    start_dispatcher,
    stop_dispatcher,
)
from workspace_orchestrator.automation.session_runtime import attach_session
from workspace_orchestrator.models import Task, WorkflowComplexity
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


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

    def unlink_session(self, task_id: str, session_id: str) -> None:
        return None


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
        model: str | None,
        resume_session_id: str | None,
        bypass_hook_trust: bool,
    ) -> CodexExecutionResult:
        self.calls.append(
            {
                "workspace_path": workspace_path,
                "prompt": prompt,
                "sandbox": sandbox,
                "model": model,
                "resume_session_id": resume_session_id,
                "bypass_hook_trust": bypass_hook_trust,
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


def test_dispatcher_executes_unbound_in_progress_task_once(tmp_path: Path, monkeypatch) -> None:
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
    assert call["model"] is None
    assert requirement_id in str(call["prompt"])
    assert "AID-1" in str(call["prompt"])
    assert "请覆盖失败场景" in str(call["prompt"])
    assert tasks.task.status == "in_review"


def test_dispatcher_resumes_its_previous_controlled_task_session(
    tmp_path: Path, monkeypatch
) -> None:
    store, _ = _store(tmp_path)
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "pid": None,
            "status": "stopped",
            "tasks": {
                "AID-1": {
                    "task_id": "AID-1",
                    "version": 7,
                    "result": "completed",
                    "session_id": "thread-old",
                }
            },
        },
    )
    tasks = FakeTasks(Task(id="AID-1", title="返工", status="in_progress", version=8))
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


def test_dispatcher_ends_failed_codex_session_and_blocks_task(tmp_path: Path, monkeypatch) -> None:
    store, requirement_id = _store(tmp_path)
    tasks = FakeTasks(Task(id="AID-1", title="失败任务", status="in_progress", version=2))

    class FailingExecutor(FakeExecutor):
        def execute(self, *args, **kwargs) -> CodexExecutionResult:
            attach_session(
                store,
                requirement_id,
                session_id="thread-auto",
                agent_name="codex",
                task_ids=("AID-1",),
            )
            return super().execute(*args, **kwargs)

    executor = FailingExecutor(tasks, returncode=1)
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher.configured_task_provider",
        lambda meta, root: tasks,
    )

    assert AutoDispatcher(store, executor).run_once() == "blocked"  # type: ignore[arg-type]

    assert tasks.task.status == "blocked"
    assert store.load(requirement_id)["sessions"][0]["result"] == "failed"
    assert "退出码 1" in tasks.added_comments[0]


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


def test_dispatcher_refuses_stop_while_worker_is_active(tmp_path: Path, monkeypatch) -> None:
    store = WorkspaceStore(tmp_path)
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "pid": 123,
            "status": "running",
            "tasks": {"opaque-1": {"task_id": "AID-1", "result": "dispatching"}},
        },
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher._pid_alive",
        lambda pid: pid == 123,
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher.os.kill",
        lambda pid, sig: killed.append((pid, sig)),
    )

    with pytest.raises(WorkspaceError, match="不支持运行中中断.*AID-1"):
        stop_dispatcher(store)

    assert killed == []
    assert store.read_json(store.root / "dispatcher.json")["status"] == "running"


def test_dispatcher_marks_stopping_before_signalling_idle_process(
    tmp_path: Path, monkeypatch
) -> None:
    store = WorkspaceStore(tmp_path)
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "pid": 123,
            "status": "running",
            "tasks": {},
        },
    )
    killed = False

    def pid_alive(pid: object) -> bool:
        return pid == 123 and not killed

    def kill(pid: int, sig: int) -> None:
        nonlocal killed
        assert pid == 123
        assert sig == signal.SIGTERM
        assert store.read_json(store.root / "dispatcher.json")["status"] == "stopping"
        killed = True

    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher._pid_alive",
        pid_alive,
    )
    monkeypatch.setattr("workspace_orchestrator.automation.dispatcher.os.kill", kill)

    stopped = stop_dispatcher(store)

    assert stopped["running"] is False
    assert stopped["status"] == "stopped"
    assert stopped["pid"] is None


def test_dispatcher_bypasses_hook_trust_only_for_exclusively_managed_hooks(
    tmp_path: Path,
) -> None:
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir()
    managed = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "ai-dev-os hook",
                            "commandWindows": "ai-dev-os hook",
                        }
                    ]
                }
            ]
        }
    }
    hooks_path.write_text(json.dumps(managed), encoding="utf-8")
    assert _only_managed_hooks(tmp_path) is True

    managed["hooks"]["UserPromptSubmit"][0]["hooks"].append(
        {"type": "command", "command": "custom-hook"}
    )
    hooks_path.write_text(json.dumps(managed), encoding="utf-8")
    assert _only_managed_hooks(tmp_path) is False


def test_delegation_policy_keeps_tiny_local_and_delegates_larger_work() -> None:
    assert decide_delegation(WorkflowComplexity.TINY).delegate is False
    for complexity in (
        WorkflowComplexity.NORMAL,
        WorkflowComplexity.COMPLEX,
        WorkflowComplexity.RESEARCH,
    ):
        assert decide_delegation(complexity).delegate is True


def test_main_delegate_only_persists_and_starts_dispatcher(tmp_path: Path, monkeypatch) -> None:
    store, requirement_id = _store(tmp_path)
    tasks = FakeTasks(Task(id="unused", title="unused"))
    created: list[Task] = []

    created_for: list[str] = []

    def create_task(req: str, task: Task) -> Task:
        created_for.append(req)
        value = replace(task, id="AID-2", raw_id="opaque-2", version=1)
        created.append(value)
        return value

    tasks.create_task = create_task  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.start_dispatcher",
        lambda store, *, explicit: {"status": "running", "running": True},
    )

    result = delegate_task(
        store,
        requirement_id.lower(),
        title="多文件实现",
        description="执行实现与测试",
    )

    assert result["status"] == "queued"
    assert result["task_id"] == "AID-2"
    assert created_for == [requirement_id]
    assert created[0].status == "in_progress"


@pytest.mark.parametrize(
    ("start_result", "message"),
    [
        ({"status": "disabled", "running": False}, "Dispatcher 未运行"),
        ({"status": "stale", "running": False}, "Dispatcher 未运行"),
        ({"status": "stopping", "running": True}, "Dispatcher 未运行"),
    ],
)
def test_delegate_never_creates_task_when_dispatcher_is_unavailable(
    tmp_path: Path,
    monkeypatch,
    start_result: dict[str, object],
    message: str,
) -> None:
    store, requirement_id = _store(tmp_path)
    tasks = FakeTasks(Task(id="unused", title="unused"))
    created: list[Task] = []
    tasks.create_task = lambda req, task: created.append(task)  # type: ignore[attr-defined,method-assign]
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.start_dispatcher",
        lambda store, *, explicit: start_result,
    )

    with pytest.raises(WorkspaceError, match=message):
        delegate_task(store, requirement_id, title="不会创建", description="Dispatcher 不可用")

    assert created == []


def test_delegate_reports_spawn_failure_before_creating_task(tmp_path: Path, monkeypatch) -> None:
    store, requirement_id = _store(tmp_path)
    tasks = FakeTasks(Task(id="unused", title="unused"))
    created: list[Task] = []
    tasks.create_task = lambda req, task: created.append(task)  # type: ignore[attr-defined,method-assign]
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    def fail_start(store: WorkspaceStore, *, explicit: bool) -> dict[str, object]:
        raise OSError("spawn failed")

    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.start_dispatcher",
        fail_start,
    )

    with pytest.raises(WorkspaceError, match="启动失败.*未创建"):
        delegate_task(store, requirement_id, title="不会创建", description="启动失败")

    assert created == []


def test_delegate_wraps_task_provider_failure_after_dispatcher_started(
    tmp_path: Path, monkeypatch
) -> None:
    store, requirement_id = _store(tmp_path)
    tasks = FakeTasks(Task(id="unused", title="unused"))

    def fail_create(req: str, task: Task) -> Task:
        raise TaskProviderError("priority invalid")

    tasks.create_task = fail_create  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.start_dispatcher",
        lambda store, *, explicit: {"status": "running", "running": True},
    )

    with pytest.raises(WorkspaceError, match="委派 Task 创建失败.*priority invalid"):
        delegate_task(store, requirement_id, title="不会创建", description="Provider 拒绝")


def test_cancelled_queued_task_never_starts_worker(tmp_path: Path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    tasks = FakeTasks(
        Task(
            id="AID-1",
            raw_id="opaque-1",
            title="待取消",
            status="in_progress",
            version=2,
        )
    )
    executor = FakeExecutor(tasks)
    monkeypatch.setattr(
        "workspace_orchestrator.automation.dispatcher.configured_task_provider",
        lambda meta, root: tasks,
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    assert request_cancel(store, "AID-1")["status"] == "cancel_requested"
    state = store.read_json(store.root / "dispatcher.json")
    assert "opaque-1" in state["tasks"]
    assert "AID-1" not in state["tasks"]
    assert AutoDispatcher(store, executor).run_once() == "cancelled"  # type: ignore[arg-type]
    assert executor.calls == []
    assert tasks.task.status == "blocked"


def test_worker_status_reports_one_active_and_other_task_queued(
    tmp_path: Path, monkeypatch
) -> None:
    store, _ = _store(tmp_path)
    first = Task(
        id="AID-1",
        title="运行中",
        raw_id="opaque-1",
        status="in_progress",
        binding_session_id="thread-worker",
        version=2,
    )
    second = Task(id="AID-2", title="排队中", raw_id="opaque-2", status="in_progress", version=1)
    tasks = FakeTasks(first)
    tasks.list_tasks = lambda requirement_id: (first, second)  # type: ignore[method-assign]
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "pid": None,
            "status": "stopped",
            "tasks": {
                "opaque-1": {
                    "task_id": "AID-1",
                    "result": "dispatching",
                    "session_id": "thread-worker",
                    "started_at": "now",
                }
            },
        },
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    status = worker_status(store)

    assert status["active_worker"]["task_id"] == "AID-1"
    assert status["active_worker"]["session_id"] == "thread-worker"
    assert [item["task_id"] for item in status["queued_tasks"]] == ["AID-2"]


def test_cancel_rejects_running_worker_without_overwriting_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    store, _ = _store(tmp_path)
    task = Task(
        id="AID-1",
        raw_id="opaque-1",
        title="运行中",
        status="in_progress",
        binding_session_id="thread-worker",
        version=2,
    )
    tasks = FakeTasks(task)
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "tasks": {
                "opaque-1": {
                    "task_id": "AID-1",
                    "result": "dispatching",
                    "session_id": "thread-worker",
                }
            },
        },
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    with pytest.raises(WorkspaceError, match="只能取消尚未启动"):
        request_cancel(store, "AID-1")

    state = store.read_json(store.root / "dispatcher.json")
    assert state["tasks"]["opaque-1"]["result"] == "dispatching"


def test_worker_status_never_requeues_a_cancelled_task(tmp_path: Path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    task = Task(
        id="AID-1",
        raw_id="opaque-1",
        title="已取消",
        status="in_progress",
        version=2,
    )
    tasks = FakeTasks(task)
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "tasks": {
                "opaque-1": {
                    "task_id": "AID-1",
                    "result": "cancelled",
                }
            },
        },
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    status = worker_status(store)

    assert status["active_worker"] is None
    assert status["queued_tasks"] == []


def test_worker_status_never_queues_a_task_owned_by_an_active_workspace_session(
    tmp_path: Path, monkeypatch
) -> None:
    store, requirement_id = _store(tmp_path)
    task = Task(
        id="AID-1",
        raw_id="opaque-1",
        title="前台 Session 正在执行",
        status="in_progress",
        version=2,
    )
    tasks = FakeTasks(task)
    attach_session(
        store,
        requirement_id,
        session_id="thread-interactive",
        agent_name="codex",
        task_ids=("AID-1",),
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    status = worker_status(store)

    assert status["active_worker"] is None
    assert status["queued_tasks"] == []


@pytest.mark.parametrize("result", ["completed", "blocked", "provider-unavailable"])
def test_worker_status_does_not_requeue_same_version_terminal_runtime_state(
    tmp_path: Path, monkeypatch, result: str
) -> None:
    store, _ = _store(tmp_path)
    task = Task(
        id="AID-1",
        raw_id="opaque-1",
        title="已有执行结果",
        status="in_progress",
        version=2,
    )
    tasks = FakeTasks(task)
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "tasks": {
                "opaque-1": {
                    "task_id": "AID-1",
                    "version": 2,
                    "result": result,
                }
            },
        },
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    assert worker_status(store)["queued_tasks"] == []


def test_worker_status_requeues_task_after_version_changes(tmp_path: Path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    task = Task(
        id="AID-1",
        raw_id="opaque-1",
        title="已有新修改",
        status="in_progress",
        version=3,
    )
    tasks = FakeTasks(task)
    store.write_json(
        store.root / "dispatcher.json",
        {
            "schema_version": 1,
            "tasks": {
                "opaque-1": {
                    "task_id": "AID-1",
                    "version": 2,
                    "result": "blocked",
                }
            },
        },
    )
    monkeypatch.setattr(
        "workspace_orchestrator.automation.delegation.configured_task_provider",
        lambda meta, root: tasks,
    )

    assert [item["task_id"] for item in worker_status(store)["queued_tasks"]] == ["AID-1"]
