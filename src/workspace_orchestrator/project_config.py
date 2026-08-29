"""AI Dev OS 项目级、可追踪的人类可读配置。"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .workspace import WorkspaceError

CONFIG_NAME = ".ai-dev-os.json"


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    task_provider: str | None
    task_project_id: str | None


def default_task_project_id(root: Path) -> str:
    """优先采用 Python 项目名，否则从目录名生成稳定 dashi 项目 ID。"""

    def slug(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return normalized[:48].rstrip("-") or "local"

    fingerprint = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:8]

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            name = (
                tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("name")
            )
        except (OSError, tomllib.TOMLDecodeError):
            name = None
        if isinstance(name, str) and name.strip():
            return f"{slug(name)}-{fingerprint}"
    return f"{slug(root.name)}-{fingerprint}"


def default_project_config(root: Path) -> ProjectConfig:
    return ProjectConfig("dashi", default_task_project_id(root))


def load_project_config(root: Path) -> ProjectConfig | None:
    path = root / CONFIG_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"项目配置无效：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceError(f"项目配置必须是 JSON 对象：{path}")
    if payload.get("schema_version") != 1:
        raise WorkspaceError(f"项目配置 schema_version 必须为 1：{path}")
    provider = payload.get("task_provider")
    project_id = payload.get("task_project_id")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise WorkspaceError(f"项目配置 task_provider 必须是非空字符串或 null：{path}")
    if provider not in {None, "dashi"}:
        raise WorkspaceError(f"项目配置包含 V1 不支持的 task_provider：{provider}")
    if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
        raise WorkspaceError(f"项目配置 task_project_id 必须是非空字符串或 null：{path}")
    if provider == "dashi" and project_id is None:
        raise WorkspaceError(f"dashi 项目配置缺少 task_project_id：{path}")
    return ProjectConfig(provider, project_id)


def initialized_project_config(root: Path) -> dict[str, object]:
    default = default_project_config(root)
    return {
        "schema_version": 1,
        "task_provider": default.task_provider,
        "task_project_id": default.task_project_id,
    }
