"""用户级 Global Project Registry：只保存已接入项目的最小索引。"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import user_config
from .project_config import ProjectConfig, default_task_project_id, project_display_name
from .workspace import WorkspaceError, WorkspaceStore, _file_lock, now_iso

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RegisteredProject:
    """一个已显式接入 AI Dev OS 的项目索引条目。"""

    id: str
    name: str
    path: str
    task_provider: str | None
    task_project_id: str | None
    registered_at: str
    updated_at: str

    @property
    def status(self) -> str:
        return "active" if Path(self.path).is_dir() else "missing"


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


class GlobalProjectRegistry:
    """并发安全、原子持久化的用户级项目索引。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or user_config.projects_registry_path()
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def _read_unlocked(self) -> tuple[RegisteredProject, ...]:
        if not self.path.exists():
            return ()
        payload = WorkspaceStore.read_json(self.path)
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise WorkspaceError(f"Global Project Registry schema_version 必须为 {SCHEMA_VERSION}")
        raw_projects = payload.get("projects")
        if not isinstance(raw_projects, list):
            raise WorkspaceError("Global Project Registry projects 必须是数组")
        projects: list[RegisteredProject] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(raw_projects):
            if not isinstance(item, dict):
                raise WorkspaceError(f"Global Project Registry projects[{index}] 必须是对象")
            required = ("id", "name", "path", "registered_at", "updated_at")
            if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
                raise WorkspaceError(
                    f"Global Project Registry projects[{index}] 缺少有效的必填字符串字段"
                )
            project_id = item["id"].strip()
            project_path = item["path"].strip()
            if not Path(project_path).is_absolute():
                raise WorkspaceError(f"项目 {project_id} 的 path 必须是绝对路径")
            if project_id in seen_ids:
                raise WorkspaceError(f"Global Project Registry 包含重复项目 ID：{project_id}")
            seen_ids.add(project_id)
            provider = item.get("task_provider")
            task_project_id = item.get("task_project_id")
            if provider is not None and (not isinstance(provider, str) or not provider.strip()):
                raise WorkspaceError(f"项目 {project_id} 的 task_provider 无效")
            if task_project_id is not None and (
                not isinstance(task_project_id, str) or not task_project_id.strip()
            ):
                raise WorkspaceError(f"项目 {project_id} 的 task_project_id 无效")
            projects.append(
                RegisteredProject(
                    id=project_id,
                    name=item["name"].strip(),
                    path=project_path,
                    task_provider=provider,
                    task_project_id=task_project_id,
                    registered_at=item["registered_at"],
                    updated_at=item["updated_at"],
                )
            )
        return tuple(projects)

    def _write_unlocked(self, projects: tuple[RegisteredProject, ...]) -> None:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "projects": [asdict(project) for project in sorted(projects, key=lambda item: item.id)],
        }
        WorkspaceStore.write_json(self.path, payload)

    def list(self) -> tuple[RegisteredProject, ...]:
        with _file_lock(self.lock_path):
            return self._read_unlocked()

    def show(self, project_id: str) -> RegisteredProject:
        normalized = project_id.strip()
        project = next((item for item in self.list() if item.id == normalized), None)
        if project is None:
            raise WorkspaceError(f"Global Project Registry 中没有项目：{normalized}")
        return project

    def register(self, root: Path, config: ProjectConfig) -> RegisteredProject:
        resolved = root.expanduser().resolve()
        project_id = config.project_id or config.task_project_id or default_task_project_id(resolved)
        with _file_lock(self.lock_path):
            projects = list(self._read_unlocked())
            path_conflict = next(
                (
                    item
                    for item in projects
                    if item.id != project_id and _path_key(item.path) == _path_key(resolved)
                ),
                None,
            )
            if path_conflict is not None:
                raise WorkspaceError(
                    f"项目路径 {resolved} 已登记为 {path_conflict.id}，不能猜测是否应改为 {project_id}"
                )
            current = next((item for item in projects if item.id == project_id), None)
            name = project_display_name(resolved)
            path = str(resolved)
            if current is not None and (
                current.name == name
                and current.path == path
                and current.task_provider == config.task_provider
                and current.task_project_id == config.task_project_id
            ):
                return current

            timestamp = now_iso()
            registered = RegisteredProject(
                id=project_id,
                name=name,
                path=path,
                task_provider=config.task_provider,
                task_project_id=config.task_project_id,
                registered_at=current.registered_at if current else timestamp,
                updated_at=timestamp,
            )
            projects = [item for item in projects if item.id != project_id]
            projects.append(registered)
            self._write_unlocked(tuple(projects))
            return registered

    def unregister(self, project_id: str) -> RegisteredProject:
        normalized = project_id.strip()
        with _file_lock(self.lock_path):
            projects = list(self._read_unlocked())
            removed = next((item for item in projects if item.id == normalized), None)
            if removed is None:
                raise WorkspaceError(f"Global Project Registry 中没有项目：{normalized}")
            self._write_unlocked(tuple(item for item in projects if item.id != normalized))
            return removed
