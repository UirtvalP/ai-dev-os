"""项目级 AI Dev OS 配置。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import WorkspaceError

CONFIG_NAME = ".ai-dev-os.json"
AUTO_FINISH_OPTION = "auto_finish_pushed_thread"


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Runtime 需要的最小项目配置视图。"""

    auto_finish_pushed_thread: bool


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"AI Dev OS 项目配置无效：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceError(f"AI Dev OS 项目配置必须是 JSON 对象：{path}")
    automation = payload.get("automation", {})
    if not isinstance(automation, dict):
        raise WorkspaceError(f"AI Dev OS automation 配置必须是 JSON 对象：{path}")
    enabled = automation.get(AUTO_FINISH_OPTION, True)
    if not isinstance(enabled, bool):
        raise WorkspaceError(
            f"AI Dev OS automation.{AUTO_FINISH_OPTION} 必须是布尔值：{path}"
        )
    return payload


def validate_project_config(path: Path) -> None:
    """在 init 写入任何文件前验证已有配置。"""

    if path.exists():
        if not path.is_file():
            raise WorkspaceError(f"目标不是普通文件：{path}")
        _read_payload(path)


def ensure_project_config(path: Path) -> str:
    """幂等创建配置，并保留用户显式关闭的选择。"""

    if path.exists():
        payload = _read_payload(path)
        automation = payload.setdefault("automation", {})
        assert isinstance(automation, dict)
        if AUTO_FINISH_OPTION in automation:
            return "preserved"
        automation[AUTO_FINISH_OPTION] = True
        outcome = "updated"
    else:
        payload = {
            "version": 1,
            "automation": {AUTO_FINISH_OPTION: True},
        }
        outcome = "created"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return outcome


def load_project_config(root: Path) -> ProjectConfig | None:
    """仅把配置文件存在的项目视为已通过 ai-dev-os init 接入。"""

    path = root / CONFIG_NAME
    if not path.is_file():
        return None
    payload = _read_payload(path)
    automation = payload.get("automation", {})
    assert isinstance(automation, dict)
    return ProjectConfig(
        auto_finish_pushed_thread=bool(automation.get(AUTO_FINISH_OPTION, True))
    )
