"""检查已注册项目不会通过项目文件接管原生 Agent 生命周期。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .project_init import AGENTS_END, AGENTS_START
from .workspace import WorkspaceError

_MANAGED_COMMAND_MARKERS = (
    "ai-dev-os hook",
    "workspace bootstrap",
    "workspace_runtime.py",
    "workspace_orchestrator.codex_hook",
    "workspace_orchestrator.hook_runtime",
)


@dataclass(frozen=True, slots=True)
class NativeIsolationReport:
    project_root: str
    isolated: bool
    checked_surfaces: tuple[str, ...]
    violations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit_native_isolation(root: Path) -> NativeIsolationReport:
    """只读审计 Codex/Claude/Cursor 会自动消费的项目级入口。"""

    project_root = root.expanduser().resolve()
    if not project_root.is_dir():
        raise WorkspaceError(f"项目目录不存在：{project_root}")
    violations: list[str] = []
    checked: list[str] = []

    agents = project_root / "AGENTS.md"
    checked.append("AGENTS.md")
    if agents.is_file():
        text = agents.read_text(encoding="utf-8")
        if AGENTS_START in text or AGENTS_END in text or _contains_managed_text(text):
            violations.append("AGENTS.md 包含 AI Dev OS 托管指令")

    for relative in (Path("CLAUDE.md"), Path(".claude/CLAUDE.md"), Path(".cursorrules")):
        checked.append(relative.as_posix())
        path = project_root / relative
        if path.is_file() and _contains_managed_text(path.read_text(encoding="utf-8")):
            violations.append(f"{relative.as_posix()} 包含 AI Dev OS 自动指令")

    for relative in (
        Path(".codex/hooks.json"),
        Path(".claude/settings.json"),
        Path(".claude/settings.local.json"),
    ):
        checked.append(relative.as_posix())
        path = project_root / relative
        if path.is_file() and _contains_managed_command(_read_json(path)):
            violations.append(f"{relative.as_posix()} 包含 AI Dev OS 生命周期命令")

    cursor_rules = project_root / ".cursor" / "rules"
    checked.append(".cursor/rules/")
    if cursor_rules.is_dir():
        for path in sorted(cursor_rules.rglob("*.mdc")):
            if _contains_managed_text(path.read_text(encoding="utf-8")):
                violations.append(
                    f"{path.relative_to(project_root).as_posix()} 包含 AI Dev OS 自动指令"
                )

    return NativeIsolationReport(
        str(project_root), not violations, tuple(checked), tuple(violations),
    )


def require_native_isolation(root: Path) -> NativeIsolationReport:
    report = audit_native_isolation(root)
    if not report.isolated:
        raise WorkspaceError("Native Isolation 失败：" + "；".join(report.violations))
    return report


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"Native Agent 配置不是有效 JSON：{path}：{exc}") from exc


def _contains_managed_command(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_managed_command(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_managed_command(item) for item in value)
    return isinstance(value, str) and _contains_managed_text(value)


def _contains_managed_text(value: str) -> bool:
    lowered = value.lower()
    return (
        any(marker in lowered for marker in _MANAGED_COMMAND_MARKERS)
        or ("bootstrap" in lowered and "workspace.exe" in lowered)
    )
