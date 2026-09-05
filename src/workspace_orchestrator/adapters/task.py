"""通过公开 JSON CLI 边界接入的可选 dashi-taskboard 适配器。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import urlopen

from workspace_orchestrator.adapters.base import TaskProviderError
from workspace_orchestrator.models import ReviewApprovalFact, Task

CommandRunner = Callable[[Sequence[str]], str]
ServiceStarter = Callable[[], None]
ActivityReader = Callable[[str], Any]


def _taskboard_endpoint() -> tuple[str, int]:
    url = os.environ.get("CODEX_TASKBOARD_URL", "http://127.0.0.1:47823")
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 47823


def _default_activity_reader(task_id: str) -> Any:
    base_url = os.environ.get("CODEX_TASKBOARD_URL", "http://127.0.0.1:47823").rstrip("/")
    url = f"{base_url}/api/tasks/{quote(task_id, safe='')}/activities"
    try:
        with urlopen(url, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPError, URLError, json.JSONDecodeError) as exc:
        raise TaskProviderError(f"无法读取 dashi Review 活动事实：{exc}") from exc


def _service_is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _taskboard_launcher() -> Path | None:
    configured = os.environ.get("CODEX_TASKBOARD_LAUNCHER")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    discovered = shutil.which("dashi-taskboard")
    if discovered:
        return Path(discovered)
    suffix = ".cmd" if os.name == "nt" else ""
    user_install = Path.home() / ".local" / "bin" / f"dashi-taskboard{suffix}"
    return user_install if user_install.is_file() else None


def ensure_taskboard_service() -> None:
    """若本机 dashi 尚未运行，则以后台进程按需启动并等待端口就绪。"""

    host, port = _taskboard_endpoint()
    if _service_is_listening(host, port):
        return
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise TaskProviderError(f"远程 dashi-taskboard 不可用：{host}:{port}")
    launcher = _taskboard_launcher()
    if launcher is None:
        raise TaskProviderError(
            "dashi-taskboard 未运行，且未找到启动器；请安装启动器或设置 CODEX_TASKBOARD_LAUNCHER"
        )
    environment = os.environ.copy()
    environment["CODEX_TASKBOARD_HOST"] = "127.0.0.1"
    environment["CODEX_TASKBOARD_PORT"] = str(port)
    if sys.platform == "win32":
        command = (os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher))
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            (str(launcher),),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _service_is_listening(host, port):
            return
        time.sleep(0.1)
    raise TaskProviderError(f"dashi-taskboard 启动超时：{host}:{port}")


def _default_runner(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            command, text=True, encoding="utf-8", capture_output=True, check=False
        )
    except OSError as exc:
        raise TaskProviderError(f"taskctl 不可用：{exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "未知的 taskctl 错误"
        raise TaskProviderError(message)
    return result.stdout


def _relation_ids(values: Sequence[object]) -> tuple[str, ...]:
    return tuple(
        str(value.get("identifier") or value.get("id")) if isinstance(value, dict) else str(value)
        for value in values
    )


def _task(data: dict[str, Any]) -> Task:
    development = data.get("developmentContext") or {}
    # schema v2 的顶层 threadId / legacyLocalThreadId 只表示 conversation
    # attribution；只有显式 threadBinding 才代表当前执行绑定。旧 payload
    # 完全没有 threadBinding 字段时，才回退读取顶层字段以保持兼容。
    has_explicit_thread_binding = "threadBinding" in data
    raw_thread_binding = data.get("threadBinding")
    thread_binding = raw_thread_binding if isinstance(raw_thread_binding, dict) else {}
    relations = data.get("relations") or {}
    refs = data.get("conversationRefs") or []
    session_ids = tuple(dict.fromkeys(str(ref["threadId"]) for ref in refs if ref.get("threadId")))
    parent = relations.get("parent")
    parent_id = None
    if parent:
        parent_id = (
            str(parent.get("identifier") or parent.get("id"))
            if isinstance(parent, dict)
            else str(parent)
        )
    return Task(
        id=str(data.get("identifier") or data["id"]),
        raw_id=str(data["id"]),
        project_id=data.get("projectId"),
        title=str(data["title"]),
        description=str(data.get("description", "")),
        status=str(data.get("status", "todo")),
        priority=data.get("priority"),
        parent_id=parent_id,
        blocked_by=_relation_ids(relations.get("blockedBy", ())),
        blocks=_relation_ids(relations.get("blocks", ())),
        branch=development.get("branch"),
        worktree=development.get("path") if development.get("type") == "worktree" else None,
        session_ids=session_ids,
        binding_session_id=(
            thread_binding.get("threadId")
            if has_explicit_thread_binding
            else data.get("threadId") or data.get("legacyLocalThreadId")
        ),
        binding_codex_project_id=thread_binding.get("codexProjectId"),
        binding_codex_project_kind=thread_binding.get("codexProjectKind"),
        binding_codex_host_id=thread_binding.get("codexHostId"),
        binding_workspace_path=thread_binding.get("workspacePath"),
        labels=tuple(data.get("labels", ())),
        version=data.get("version"),
        activity_updated_at=data.get("activityUpdatedAt") or data.get("updatedAt"),
    )


def _default_executable() -> str:
    configured = os.environ.get("DASHI_TASKCTL")
    if configured:
        return configured
    discovered = shutil.which("taskctl")
    if discovered:
        return discovered
    user_install = Path.home() / ".local" / "bin" / "taskctl.cmd"
    if user_install.is_file():
        return str(user_install)
    return "taskctl"


@dataclass(slots=True)
class DashiTaskProvider:
    """v1.1 JSON 契约；dashi 不可用时不得破坏工作区状态。"""

    project_id: str = "local"
    runner: CommandRunner = _default_runner
    executable: str | None = None
    service_starter: ServiceStarter | None = None
    project_name: str | None = None
    workspace_path: str | None = None
    activity_reader: ActivityReader = _default_activity_reader
    _service_ready: bool = False
    _project_ready: bool = False

    def _json(self, *args: str, ensure_project: bool = True) -> Any:
        if self.service_starter is not None and not self._service_ready:
            self.service_starter()
            self._service_ready = True
        if ensure_project and args and args[0] != "project" and self.workspace_path:
            self.ensure_project()
        command = (self.executable or _default_executable(), *args, "--json")
        output = self.runner(command)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise TaskProviderError("taskctl 返回了无效 JSON") from exc

    def ensure_project(self) -> None:
        """幂等创建或映射当前 Workspace 对应的 dashi 项目。"""

        if self._project_ready or not self.workspace_path:
            return
        payload = self._json("project", "list", ensure_project=False)
        projects = payload.get("projects", ())
        current = next(
            (item for item in projects if str(item.get("id")) == self.project_id),
            None,
        )
        if current is None:
            self._json(
                "project",
                "create",
                "--name",
                self.project_name or self.project_id,
                "--id",
                self.project_id,
                "--workspace-path",
                self.workspace_path,
                ensure_project=False,
            )
        elif current.get("workspacePath") is None:
            self._json(
                "project",
                "map",
                self.project_id,
                "--workspace-path",
                self.workspace_path,
                ensure_project=False,
            )
        elif os.path.normcase(os.path.abspath(str(current["workspacePath"]))) != os.path.normcase(
            os.path.abspath(self.workspace_path)
        ):
            raise TaskProviderError(
                f"dashi 项目 ID {self.project_id} 已映射到其他目录：{current['workspacePath']}"
            )
        self._project_ready = True

    @staticmethod
    def _requirement_label(requirement_id: str) -> str:
        return f"requirement:{requirement_id}"

    def create_task(self, requirement_id: str, task: Task) -> Task:
        args = [
            "issue",
            "create",
            "--project",
            self.project_id,
            "--title",
            task.title,
            "--description",
            task.description,
            "--status",
            task.status,
            "--labels",
            ",".join(dict.fromkeys((*task.labels, self._requirement_label(requirement_id)))),
        ]
        if task.priority:
            args.extend(("--priority", task.priority))
        if task.worktree:
            if not task.branch:
                raise TaskProviderError("工作树任务必须指定分支")
            args.extend(("--worktree-path", task.worktree, "--worktree-branch", task.branch))
        elif task.branch:
            args.extend(("--git-branch", task.branch))
        return _task(self._json(*args)["task"])

    def get_task(self, task_id: str) -> Task:
        return _task(self._json("issue", "get", task_id)["task"])

    def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
        payload = self._json("issue", "list", "--project", self.project_id)
        required_label = self._requirement_label(requirement_id)
        return tuple(
            _task(item) for item in payload["tasks"] if required_label in item.get("labels", ())
        )

    def update_status(self, task_id: str, status: str) -> Task:
        current = self.get_task(task_id)
        args = ["issue", "move", task_id, "--status", status]
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        return _task(self._json(*args)["task"])

    def publish_review(self, task_id: str, content: str) -> Task:
        """用卡片正文幂等投影 Review Packet，并通过 version 做 CAS。"""

        for _ in range(2):
            current = self.get_task(task_id)
            if current.description == content:
                return current
            descriptor, name = tempfile.mkstemp(suffix=".md")
            path = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                args = ["issue", "update", task_id, "--description-file", str(path)]
                if current.version is not None:
                    args.extend(("--if-version", str(current.version)))
                try:
                    return _task(self._json(*args)["task"])
                except TaskProviderError:
                    refreshed = self.get_task(task_id)
                    if refreshed.description == content:
                        return refreshed
                    if refreshed.version == current.version:
                        raise
            finally:
                path.unlink(missing_ok=True)
        raise TaskProviderError(f"Review Packet 并发更新未收敛：{task_id}")

    def review_approval_fact(self, task_id: str) -> ReviewApprovalFact | None:
        """读取最后一次进入 done 的结构化 actor；缺失事实绝不猜测。"""

        if self.service_starter is not None and not self._service_ready:
            self.service_starter()
            self._service_ready = True
        payload = self.activity_reader(task_id)
        activities = payload.get("activities", ()) if isinstance(payload, dict) else ()
        for activity in reversed(tuple(activities)):
            if not isinstance(activity, dict):
                continue
            changes = activity.get("changes", ())
            entered_done = any(
                isinstance(change, dict)
                and change.get("field") == "status"
                and change.get("after") == "done"
                for change in changes
            )
            if not entered_done:
                continue
            activity_id = str(activity.get("id") or "").strip()
            actor_type = str(activity.get("actorType") or "").strip()
            changed_at = str(activity.get("createdAt") or "").strip()
            if not activity_id or not actor_type or not changed_at:
                return None
            return ReviewApprovalFact(
                activity_id=activity_id,
                actor_type=actor_type,
                actor_id=str(activity.get("actorId") or "").strip(),
                actor_name=str(activity.get("actorName") or "").strip(),
                changed_at=changed_at,
            )
        return None

    def add_comment(self, task_id: str, body: str) -> None:
        self._json("comment", "add", task_id, "--body", body)

    def list_comments(self, task_id: str) -> tuple[str, ...]:
        payload = self._json("comment", "list", task_id)
        comments = payload.get("comments", ())
        return tuple(
            str(item.get("body") or item.get("content") or "").strip()
            for item in comments
            if isinstance(item, dict) and str(item.get("body") or item.get("content") or "").strip()
        )

    def link_session(
        self,
        task_id: str,
        session_id: str,
        *,
        codex_project_id: str | None = None,
        codex_project_kind: str | None = None,
        codex_host_id: str | None = None,
        workspace_path: str | None = None,
    ) -> None:
        current = self.get_task(task_id)
        args = [
            "issue",
            "move",
            task_id,
            "--status",
            current.status,
            "--binding-thread-id",
            session_id,
        ]
        binding = (codex_project_id, codex_project_kind, codex_host_id, workspace_path)
        if any(binding) and not all(binding):
            raise TaskProviderError("完整的 Codex 任务绑定必须包含全部四个身份字段")
        if all(binding):
            args.extend(
                (
                    "--binding-codex-project-id",
                    str(codex_project_id),
                    "--binding-codex-project-kind",
                    str(codex_project_kind),
                    "--binding-codex-host-id",
                    str(codex_host_id),
                    "--binding-workspace-path",
                    str(workspace_path),
                )
            )
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        self._json(*args)

    def unlink_session(self, task_id: str, session_id: str) -> None:
        """清除 dashi 的当前绑定；历史 Task 归属由 Workspace Session 保留。"""

        current = self.get_task(task_id)
        if current.binding_session_id != session_id:
            return
        args = [
            "issue",
            "move",
            task_id,
            "--status",
            current.status,
            "--clear-binding-thread",
        ]
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        self._json(*args)

    def set_git_context(
        self, task_id: str, branch: str | None = None, worktree: str | None = None
    ) -> None:
        if worktree and not branch:
            raise TaskProviderError("工作树绑定必须指定分支")
        if not branch:
            raise TaskProviderError("Git 上下文必须包含分支或工作树")
        current = self.get_task(task_id)
        args = ["issue", "update", task_id]
        if worktree:
            args.extend(("--worktree-path", worktree, "--worktree-branch", branch))
        else:
            args.extend(("--git-branch", branch))
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        self._json(*args)

    def set_parent(self, task_id: str, parent_id: str) -> None:
        current = self.get_task(task_id)
        if current.parent_id == parent_id:
            return
        args = ["issue", "relation", "add", task_id, "--type", "parent", "--issue", parent_id]
        if current.version is not None:
            args.extend(("--if-version", str(current.version)))
        self._json(*args)
