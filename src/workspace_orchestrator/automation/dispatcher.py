"""从 dashi `in_progress` 状态反向触发 Codex 的本地单任务 Dispatcher。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace_orchestrator.adapters.agent import CodexExecProvider, CodexExecutionResult
from workspace_orchestrator.adapters.base import TaskProvider, TaskProviderError
from workspace_orchestrator.models import Task
from workspace_orchestrator.project_config import (
    ProjectConfig,
    default_project_config,
    load_project_config,
)
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore, now_iso

from .task_attach import configured_task_provider, is_requirement_review_task

STATE_FILE = "dispatcher.json"
LOG_DIRECTORY = "dispatcher-logs"


@dataclass(frozen=True, slots=True)
class DispatchCandidate:
    requirement_id: str
    task: Task
    task_provider: TaskProvider
    execution_path: Path
    resume_session_id: str | None
    comments: tuple[str, ...]


def _state_path(store: WorkspaceStore) -> Path:
    return store.root / STATE_FILE


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pid": None,
        "status": "stopped",
        "updated_at": now_iso(),
        "tasks": {},
    }


def _read_state(store: WorkspaceStore) -> dict[str, Any]:
    path = _state_path(store)
    if not path.is_file():
        return _empty_state()
    try:
        value = store.read_json(path)
    except WorkspaceError:
        return _empty_state()
    return value if isinstance(value, dict) else _empty_state()


def _write_state(store: WorkspaceStore, state: dict[str, Any]) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    store.write_json(_state_path(store), state)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _config(store: WorkspaceStore) -> ProjectConfig:
    return (
        load_project_config(store.working_root)
        or load_project_config(store.project_root)
        or default_project_config(store.project_root)
    )


def _task_key(task: Task) -> str:
    return task.raw_id or task.id


def _active_workspace_session(
    store: WorkspaceStore, requirement_id: str, task_id: str
) -> bool:
    return any(
        item.get("result") == "in_progress" and task_id in item.get("task_ids", ())
        for item in store.load(requirement_id)["sessions"]
    )


def _resume_session_id(
    store: WorkspaceStore, requirement_id: str, task_id: str
) -> str | None:
    for item in reversed(store.load(requirement_id)["sessions"]):
        if task_id in item.get("task_ids", ()) and item.get("result") != "in_progress":
            session_id = str(item.get("id") or "").strip()
            if session_id:
                return session_id
    return None


def _execution_path(store: WorkspaceStore, task: Task, meta: dict[str, Any]) -> Path:
    configured = task.worktree or dict(meta.get("git") or {}).get("worktree")
    if not configured:
        return store.working_root.resolve()
    path = Path(str(configured)).expanduser()
    return (path if path.is_absolute() else store.project_root / path).resolve()


def _prompt(candidate: DispatchCandidate) -> str:
    comments = "\n".join(f"- {item}" for item in candidate.comments) or "无"
    description = candidate.task.description.strip() or "无补充描述"
    return (
        "你由 AI Dev OS 本地 Dispatcher 自动启动。\n"
        f"继续 {candidate.requirement_id}，处理 Task {candidate.task.id}："
        f"{candidate.task.title}\n\n"
        f"任务描述：\n{description}\n\n"
        f"任务评论：\n{comments}\n\n"
        "把这些内容视为本轮用户开发请求，遵守仓库 AGENTS.md、Workspace Context Snapshot "
        "与 Requirement Intent。完成实现和验证后只调用一次 workspace finalize；"
        "不要手工重复 Hook 已负责的绑定、Task 或 Session 步骤。"
    )


def _result_summary(result: CodexExecutionResult) -> str:
    detail = result.stderr.strip()
    if not detail:
        messages: list[str] = []
        for line in result.stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = payload.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))
        detail = messages[-1] if messages else "Codex 未返回可读结果"
    return detail[-4000:]


@dataclass(slots=True)
class AutoDispatcher:
    """单进程、单任务认领；不调度多个 Agent，也不抢占已有绑定。"""

    store: WorkspaceStore
    executor: CodexExecProvider

    def _task_state(self, task: Task) -> dict[str, Any] | None:
        state = _read_state(self.store)
        value = dict(state.get("tasks") or {}).get(_task_key(task))
        return value if isinstance(value, dict) else None

    def _remember(self, task: Task, **changes: object) -> None:
        with self.store.locked():
            state = _read_state(self.store)
            tasks = dict(state.get("tasks") or {})
            current = dict(tasks.get(_task_key(task)) or {})
            current.update(
                task_id=task.id,
                raw_id=task.raw_id,
                version=task.version,
                activity_updated_at=task.activity_updated_at,
                updated_at=now_iso(),
                **changes,
            )
            tasks[_task_key(task)] = current
            state["tasks"] = tasks
            _write_state(self.store, state)

    def _candidate(self) -> DispatchCandidate | None:
        config = _config(self.store)
        if config.task_provider != "dashi" or not config.auto_execute_in_progress:
            return None
        for requirement_id in self.store.requirement_ids(
            statuses={"draft", "ready", "in_progress", "blocked"}
        ):
            data = self.store.load(requirement_id)
            provider = configured_task_provider(data["meta"], self.store.project_root)
            if provider is None:
                continue
            try:
                tasks = provider.list_tasks(requirement_id)
            except TaskProviderError:
                continue
            for task in tasks:
                if (
                    task.status != "in_progress"
                    or is_requirement_review_task(task)
                    or task.binding_session_id
                    or _active_workspace_session(self.store, requirement_id, task.id)
                ):
                    continue
                previous = self._task_state(task)
                if (
                    previous
                    and previous.get("version") == task.version
                    and previous.get("result") != "dispatching"
                ):
                    continue
                path = _execution_path(self.store, task, data["meta"])
                try:
                    comments = tuple(provider.list_comments(task.id))
                except TaskProviderError:
                    comments = ()
                return DispatchCandidate(
                    requirement_id=requirement_id,
                    task=task,
                    task_provider=provider,
                    execution_path=path,
                    resume_session_id=_resume_session_id(
                        self.store, requirement_id, task.id
                    ),
                    comments=comments,
                )
        return None

    def _record_log(
        self, candidate: DispatchCandidate, result: CodexExecutionResult
    ) -> str:
        directory = self.store.root / LOG_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = candidate.task.id.replace("/", "-").replace("\\", "-")
        path = directory / f"{safe_id}-{int(time.time())}.json"
        self.store.write_json(
            path,
            {
                "requirement_id": candidate.requirement_id,
                "task_id": candidate.task.id,
                "started_from": str(candidate.execution_path),
                "resumed": result.resumed,
                "session_id": result.session_id,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "finished_at": now_iso(),
            },
        )
        return str(path.relative_to(self.store.project_root))

    def run_once(self) -> str:
        candidate = self._candidate()
        if candidate is None:
            return "idle"
        task = candidate.task
        if not candidate.execution_path.is_dir():
            message = f"自动执行失败：任务工作目录不存在：{candidate.execution_path}"
            try:
                candidate.task_provider.add_comment(task.id, message)
                refreshed = candidate.task_provider.update_status(task.id, "blocked")
            except TaskProviderError:
                refreshed = task
            self._remember(refreshed, result="blocked", error=message)
            return "blocked"

        self._remember(task, result="dispatching", requirement_id=candidate.requirement_id)
        result = self.executor.execute(
            candidate.execution_path,
            _prompt(candidate),
            sandbox=_config(self.store).codex_sandbox,
            resume_session_id=candidate.resume_session_id,
        )
        log_path = self._record_log(candidate, result)
        try:
            refreshed = candidate.task_provider.get_task(task.id)
        except TaskProviderError:
            self._remember(
                task,
                result="provider-unavailable",
                session_id=result.session_id,
                log=log_path,
            )
            return "provider-unavailable"

        if result.returncode != 0:
            message = (
                f"自动 Codex 执行失败（退出码 {result.returncode}）。\n\n"
                f"{_result_summary(result)}\n\n本地日志：{log_path}"
            )
            try:
                candidate.task_provider.add_comment(task.id, message)
                refreshed = candidate.task_provider.update_status(task.id, "blocked")
            except TaskProviderError:
                pass
            self._remember(
                refreshed,
                result="blocked",
                session_id=result.session_id,
                log=log_path,
                error=_result_summary(result),
            )
            return "blocked"

        if refreshed.status == "in_progress":
            message = (
                "Codex 自动执行进程已经结束，但 Task 未进入 review；为避免看似仍在处理，"
                f"已转为 blocked。\n\n{_result_summary(result)}\n\n本地日志：{log_path}"
            )
            try:
                candidate.task_provider.add_comment(task.id, message)
                refreshed = candidate.task_provider.update_status(task.id, "blocked")
            except TaskProviderError:
                pass
            outcome = "blocked"
        else:
            outcome = "completed"
        self._remember(
            refreshed,
            result=outcome,
            session_id=result.session_id,
            log=log_path,
        )
        return outcome


def dispatcher_status(store: WorkspaceStore) -> dict[str, Any]:
    state = _read_state(store)
    alive = _pid_alive(state.get("pid"))
    state["running"] = alive
    if not alive and state.get("status") in {"starting", "running"}:
        state["status"] = "stale"
    return state


def start_dispatcher(store: WorkspaceStore, *, explicit: bool = False) -> dict[str, Any]:
    config = _config(store)
    if config.task_provider != "dashi" or not config.auto_execute_in_progress:
        return {"status": "disabled", "running": False}
    if not explicit and os.environ.get("AI_DEV_OS_DISABLE_AUTOSTART") == "1":
        return {"status": "disabled-by-environment", "running": False}
    store.root.mkdir(parents=True, exist_ok=True)
    log_path = store.root / "dispatcher-process.log"
    environment = os.environ.copy()
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI"):
        environment.pop(name, None)
    environment["AI_DEV_OS_DISPATCHER_CHILD"] = "1"
    command = (
        sys.executable,
        "-m",
        "workspace_orchestrator.product_cli",
        "dispatcher",
        "serve",
        "--root",
        str(store.project_root.resolve()),
    )
    creationflags = 0
    popen_options: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        popen_options = {"creationflags": creationflags}
    with store.locked():
        current = dispatcher_status(store)
        if current.get("running"):
            return current
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=store.working_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                **popen_options,
            )
        state = _read_state(store)
        state.update(pid=process.pid, status="starting", started_at=now_iso())
        _write_state(store, state)
    return dispatcher_status(store)


def stop_dispatcher(store: WorkspaceStore) -> dict[str, Any]:
    state = _read_state(store)
    pid = state.get("pid")
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    with store.locked():
        state = _read_state(store)
        state.update(pid=None, status="stopped", stopped_at=now_iso())
        _write_state(store, state)
    return dispatcher_status(store)


def serve_dispatcher(store: WorkspaceStore) -> int:
    stop_event = threading.Event()

    def request_stop(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, request_stop)
    with store.locked():
        state = _read_state(store)
        state.update(pid=os.getpid(), status="running", started_at=now_iso())
        _write_state(store, state)
    dispatcher = AutoDispatcher(store, CodexExecProvider())
    try:
        while not stop_event.is_set():
            dispatcher.run_once()
            stop_event.wait(_config(store).dispatcher_poll_seconds)
    finally:
        with store.locked():
            state = _read_state(store)
            if state.get("pid") == os.getpid():
                state.update(pid=None, status="stopped", stopped_at=now_iso())
                _write_state(store, state)
    return 0
