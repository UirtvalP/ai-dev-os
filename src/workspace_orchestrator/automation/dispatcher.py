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
from typing import Any, TypeGuard, cast

from workspace_orchestrator.adapters.agent import CodexExecProvider, CodexExecutionResult
from workspace_orchestrator.adapters.base import TaskProvider, TaskProviderError
from workspace_orchestrator.models import Task
from workspace_orchestrator.phase_gate import GateStore, PhaseGateError
from workspace_orchestrator.project_config import (
    ProjectConfig,
    default_project_config,
    load_project_config,
)
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore, now_iso

from .session_runtime import end_session
from .task_attach import (
    configured_task_provider,
    is_requirement_review_task,
    is_requirement_space_task,
)

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


def _pid_alive(pid: object) -> TypeGuard[int]:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # Windows 的 os.kill(pid, 0) 在已退出进程上仍可能成功；直接读取
        # process exit code，避免把 stopping/stale PID 永久误判为存活。
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        open_process.restype = ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        get_exit_code.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        handle = open_process(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            close_handle(handle)
    # start_dispatcher 与 stop_dispatcher 在同一进程内运行时（例如集成
    # 测试），已退出的子进程会先进入 zombie 状态。os.kill(pid, 0)
    # 仍会把 zombie 视为存活，因此先以 WNOHANG 回收直接子进程。
    # 独立 CLI 调用并非该 PID 的父进程，os.waitpid 会抛
    # ChildProcessError，随后仍使用通用的存活探测。
    try:
        posix_os = cast(Any, os)
        waited_pid, _ = posix_os.waitpid(pid, posix_os.WNOHANG)
    except (AttributeError, ChildProcessError, OSError):
        pass
    else:
        if waited_pid == pid:
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


def _task_state_allows_dispatch(task: Task, previous: dict[str, Any] | None) -> bool:
    """同一 Task version 的终态不得在状态查询中重新伪装成 queued。"""

    return not (
        previous
        and previous.get("version") == task.version
        and previous.get("result") not in {"dispatching", "cancel_requested"}
    )


def _active_workspace_session(
    store: WorkspaceStore, requirement_id: str, task_id: str
) -> bool:
    return any(
        item.get("result") == "in_progress" and task_id in item.get("task_ids", ())
        for item in store.load(requirement_id)["sessions"]
    )


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


def _only_managed_hooks(workspace_path: Path) -> bool:
    """只有全部 Hook 都是 ai-dev-os 托管命令时才允许跳过首次信任提示。"""

    path = workspace_path / ".codex" / "hooks.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    groups = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(groups, dict) or not groups:
        return False
    commands: list[str] = []
    for entries in groups.values():
        if not isinstance(entries, list):
            return False
        for group in entries:
            hooks = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(hooks, list):
                return False
            for hook in hooks:
                if not isinstance(hook, dict):
                    return False
                for field in ("command", "commandWindows"):
                    if hook.get(field):
                        commands.append(str(hook[field]).strip())
    return bool(commands) and all(command == "ai-dev-os hook" for command in commands)


def _result_summary(result: CodexExecutionResult) -> str:
    messages: list[str] = []
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = payload.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            messages.append(str(item["text"]))
        elif payload.get("type") == "error" and payload.get("message"):
            messages.append(str(payload["message"]))
        elif payload.get("type") == "turn.failed":
            error = payload.get("error") or {}
            if error.get("message"):
                messages.append(str(error["message"]))
    detail = messages[-1] if messages else result.stderr.strip()
    return (detail or "Codex 未返回可读结果")[-1800:]


def _block_task(provider: TaskProvider, task: Task, message: str) -> Task:
    """评论失败也必须继续改状态，避免卡片永久伪装成处理中。"""

    try:
        provider.add_comment(task.id, message)
    except TaskProviderError:
        pass
    try:
        return provider.update_status(task.id, "blocked")
    except TaskProviderError:
        try:
            return provider.get_task(task.id)
        except TaskProviderError:
            return task


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

    def _claim(self, task: Task, requirement_id: str) -> bool:
        """与 queued cancel 共用同一文件锁，保证启动边界只有一方获胜。"""

        GateStore(self.store).require_task_active(requirement_id, task.id)
        with self.store.locked():
            state = _read_state(self.store)
            tasks = dict(state.get("tasks") or {})
            current = dict(tasks.get(_task_key(task)) or {})
            if current.get("result") == "cancel_requested":
                return False
            current.update(
                task_id=task.id,
                raw_id=task.raw_id,
                version=task.version,
                activity_updated_at=task.activity_updated_at,
                updated_at=now_iso(),
                result="dispatching",
                requirement_id=requirement_id,
            )
            tasks[_task_key(task)] = current
            state["tasks"] = tasks
            _write_state(self.store, state)
        return True

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
                    or is_requirement_space_task(task)
                    or task.binding_session_id
                    or _active_workspace_session(self.store, requirement_id, task.id)
                ):
                    continue
                previous = self._task_state(task)
                if not _task_state_allows_dispatch(task, previous):
                    continue
                try:
                    GateStore(self.store).require_task_active(requirement_id, task.id)
                except PhaseGateError as exc:
                    refreshed = _block_task(
                        provider,
                        task,
                        f"阶段门禁拒绝 Dispatcher 启动：{exc}",
                    )
                    self._remember(refreshed, result="blocked", error=str(exc))
                    raise
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
                    # 只恢复 Dispatcher 自己以受控 sandbox 启动过的 Session；
                    # 不恢复权限配置未知的交互式 Desktop Thread。
                    resume_session_id=(
                        str(previous.get("session_id"))
                        if previous and previous.get("session_id")
                        else None
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
        try:
            candidate = self._candidate()
        except PhaseGateError:
            return "blocked"
        if candidate is None:
            return "idle"
        task = candidate.task
        try:
            claimed = self._claim(task, candidate.requirement_id)
        except PhaseGateError as exc:
            refreshed = _block_task(
                candidate.task_provider,
                task,
                f"阶段门禁在认领前失效：{exc}",
            )
            self._remember(refreshed, result="blocked", error=str(exc))
            return "blocked"
        if not claimed:
            refreshed = _block_task(candidate.task_provider, task, "任务已按 Main 请求取消，未启动 Worker。")
            self._remember(refreshed, result="cancelled")
            return "cancelled"
        if not candidate.execution_path.is_dir():
            message = f"自动执行失败：任务工作目录不存在：{candidate.execution_path}"
            refreshed = _block_task(candidate.task_provider, task, message)
            self._remember(refreshed, result="blocked", error=message)
            return "blocked"

        result = self.executor.execute(
            candidate.execution_path,
            _prompt(candidate),
            sandbox=_config(self.store).codex_sandbox,
            model=_config(self.store).codex_model,
            resume_session_id=candidate.resume_session_id,
            bypass_hook_trust=_only_managed_hooks(candidate.execution_path),
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
            if result.session_id:
                end_session(
                    self.store,
                    candidate.requirement_id,
                    result.session_id,
                    result="failed",
                    task_provider=candidate.task_provider,
                )
            message = (
                f"自动 Codex 执行失败（退出码 {result.returncode}）。\n\n"
                f"{_result_summary(result)}\n\n本地日志：{log_path}"
            )
            refreshed = _block_task(candidate.task_provider, refreshed, message)
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
            refreshed = _block_task(candidate.task_provider, refreshed, message)
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
    if sys.platform == "win32":
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
    with store.locked():
        state = _read_state(store)
        pid = state.get("pid")
        alive = _pid_alive(pid)
        active_task_ids = tuple(
            str(value.get("task_id") or key)
            for key, value in dict(state.get("tasks") or {}).items()
            if isinstance(value, dict)
            if value.get("result") in {"dispatching", "running"}
        )
        if alive and active_task_ids:
            raise WorkspaceError(
                "Dispatcher 正在执行 Task，V1 不支持运行中中断："
                + ", ".join(active_task_ids)
            )
        if not alive:
            state.update(pid=None, status="stopped", stopped_at=now_iso())
            _write_state(store, state)
            return dispatcher_status(store)
        if not isinstance(pid, int):
            raise WorkspaceError("Dispatcher PID 状态无效；已拒绝发送停止信号")
        state.update(status="stopping", stop_requested_at=now_iso())
        _write_state(store, state)

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + 5.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        raise WorkspaceError("Dispatcher 停止超时；已保持 stopping 状态，未启动第二个 Worker")
    with store.locked():
        state = _read_state(store)
        if state.get("pid") == pid:
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
