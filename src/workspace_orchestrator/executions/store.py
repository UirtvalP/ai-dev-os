"""以人类可读 JSON 文件持久化 Execution；Requirement 仍是所有权边界。"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from ..workspace import WorkspaceError, WorkspaceStore, now_iso
from .models import Execution, ExecutionStatus

_EXECUTION_ID = re.compile(r"EXE-\d{6,}\Z")


class ExecutionStore:
    def __init__(self, workspace: WorkspaceStore) -> None:
        self.workspace = workspace

    def create(
        self, requirement_id: str, task_id: str, *, role: str, runtime_id: str,
        provider: str | None = None, model: str | None = None,
        reasoning_effort: str | None = None, prompt: str,
        workspace_path: Path | None = None, source: str = "workbench",
        parent_execution_id: str | None = None,
        creation_key: str | None = None,
    ) -> Execution:
        requirement_id = requirement_id.upper()
        creation_key = (creation_key or "").strip() or None
        self.workspace.load(requirement_id)
        if not task_id.strip():
            raise WorkspaceError("Execution 必须绑定非空 task_id")
        # Execution ID 属于整个 AI Dev OS 项目，而不是单个 Requirement；全局锁避免并发重复。
        with self.workspace.locked():
            root = self._root(requirement_id)
            root.mkdir(parents=True, exist_ok=True)
            if creation_key:
                for path in self.workspace.root.glob("REQ-*/executions/EXE-*.json"):
                    existing = self._read_path(path)
                    if existing.creation_key == creation_key:
                        return existing
            numbers = [
                int(match.group(1))
                for path in self.workspace.root.glob("REQ-*/executions/EXE-*.json")
                if (match := re.fullmatch(r"EXE-(\d+)\.json", path.name))
            ]
            execution_id = f"EXE-{max(numbers, default=0) + 1:06d}"
            timestamp = now_iso()
            execution = Execution(
                id=execution_id, requirement_id=requirement_id, task_id=task_id,
                role=role, runtime_id=runtime_id, provider=provider or runtime_id,
                model=model, reasoning_effort=reasoning_effort,
                workspace_path=str((workspace_path or self.workspace.working_root).resolve()),
                status="queued", created_at=timestamp, updated_at=timestamp,
                prompt=prompt, parent_execution_id=parent_execution_id, source=source,
                creation_key=creation_key,
            )
            self.workspace.write_json(root / f"{execution_id}.json", execution.to_dict())
            return execution

    def get(self, execution_id: str) -> Execution:
        if _EXECUTION_ID.fullmatch(execution_id.upper()) is None:
            raise WorkspaceError(f"无效的 Execution ID：{execution_id}")
        with self.workspace.locked():
            path = self._path_for_id(execution_id.upper())
            return self._read_path(path)

    def list(self, requirement_id: str) -> tuple[Execution, ...]:
        requirement_id = requirement_id.upper()
        self.workspace.load(requirement_id)
        root = self._root(requirement_id)
        if not root.exists():
            return ()
        with self.workspace.locked(requirement_id):
            return tuple(self._read_path(path) for path in sorted(root.glob("EXE-*.json")))

    def update(self, execution_id: str, *, status: ExecutionStatus, **changes: object) -> Execution:
        allowed = {
            "session_id", "turn_id", "branch", "worktree", "started_at",
            "completed_at", "last_progress_at", "summary", "result", "error", "event_cursor",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise WorkspaceError("不允许更新 Execution 字段：" + ", ".join(sorted(unknown)))
        with self.workspace.locked():
            path = self._path_for_id(execution_id.upper())
            current = self._read_path(path)
            updated = replace(
                current, **cast(Any, {"status": status, "updated_at": now_iso(), **changes})
            )
            self.workspace.write_json(path, updated.to_dict())
            return updated

    def prepare(
        self, execution_id: str, *, role: str, runtime_id: str, provider: str,
        model: str | None, reasoning_effort: str | None, workspace_path: Path,
        prompt: str,
    ) -> Execution:
        """在 queued/waiting → starting 边界冻结最终 Runtime 输入。"""

        with self.workspace.locked():
            path = self._path_for_id(execution_id.upper())
            current = self._read_path(path)
            if current.status not in {"queued", "waiting", "starting"}:
                raise WorkspaceError(
                    f"Execution {current.id} 当前不能准备启动：{current.status}"
                )
            updated = replace(
                current, role=role, runtime_id=runtime_id, provider=provider,
                model=model, reasoning_effort=reasoning_effort,
                workspace_path=str(workspace_path.resolve()), prompt=prompt,
                status="starting", updated_at=now_iso(),
            )
            self.workspace.write_json(path, updated.to_dict())
            return updated

    def _root(self, requirement_id: str) -> Path:
        return self.workspace.path_for(requirement_id) / "executions"

    def _path_for_id(self, execution_id: str) -> Path:
        matches = list(self.workspace.root.glob(f"REQ-*/executions/{execution_id}.json"))
        if not matches:
            raise WorkspaceError(f"未找到 Execution：{execution_id}")
        if len(matches) != 1:
            raise WorkspaceError(f"Execution ID 不唯一：{execution_id}")
        return matches[0]

    def _read_path(self, path: Path) -> Execution:
        try:
            execution = Execution.from_dict(self.workspace.read_json(path))
        except (TypeError, ValueError) as exc:
            raise WorkspaceError(f"Execution 数据损坏：{exc}") from exc
        expected_requirement = path.parent.parent.name
        if execution.id != path.stem or execution.requirement_id != expected_requirement:
            raise WorkspaceError(
                f"Execution 文件身份不匹配：{path.name} / {expected_requirement}"
            )
        return execution
