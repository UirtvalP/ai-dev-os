"""将现有项目安全接入 AI Dev OS。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import user_config
from .project_config import CONFIG_NAME, initialized_project_config, load_project_config
from .workspace import WorkspaceError, WorkspaceStore

AGENTS_START = "<!-- ai-dev-os:start -->"
AGENTS_END = "<!-- ai-dev-os:end -->"
GITIGNORE_START = "# ai-dev-os:start"
GITIGNORE_END = "# ai-dev-os:end"
HOOK_COMMAND = "ai-dev-os hook"

AGENTS_BLOCK = f"""{AGENTS_START}
## AI Dev OS

- `.codex/hooks.json` 调用全局安装的 `ai-dev-os hook`，更新一次 CLI 后运行时能力即全局生效。
- Hook 动态注入的“AI Dev OS 运行时契约”和 Context Snapshot 是当前流程与状态的事实来源；
  不要复制或固化特定版本的运行时步骤。
- Hook 未启用或未受信任时，只回退执行一次 `workspace bootstrap --request "<当前开发请求>"`；
  请求包含明确的 `REQ-<数字>` 或 Task ID 时原样传入。
- 必须阅读用户级 `~/.ai-dev-os/USER_PRINCIPLES.md`、项目级 `PROJECT_INTENT.md` 和当前需求的 `intent.md`。
- 多个活动 Requirement 或多个 `in_progress` Task 存在歧义时，不得静默选择。
{AGENTS_END}
"""

PROJECT_INTENT = """# 项目意图

## 目的

请说明这个项目为何存在，以及它要为用户解决的核心问题。

## 期望结果

请说明成功时用户能够获得什么结果。

## 不得演变成

请记录项目明确不应成为的形态，以及不可突破的产品边界。

## 取舍优先级

请按优先级记录发生冲突时应如何取舍。
"""

GITIGNORE_BLOCK = f"""{GITIGNORE_START}
.workspace/
.worktrees/
{GITIGNORE_END}
"""


def _hook_group(event_name: str) -> dict[str, object]:
    hook: dict[str, object] = {
        "type": "command",
        "command": HOOK_COMMAND,
        "commandWindows": HOOK_COMMAND,
    }
    if event_name == "Stop":
        hook.update(
            statusMessage="检查已推送任务自动收尾",
            timeout=30,
        )
        hook["async"] = True
    elif event_name != "SessionEnd":
        hook.update(
            statusMessage="自动恢复 AI Dev OS Workspace",
            additionalContextLimit=5000,
        )
    else:
        hook["timeout"] = 3
    group: dict[str, object] = {"hooks": [hook]}
    if event_name == "SessionStart":
        group["matcher"] = "startup|resume"
    return group


@dataclass(frozen=True, slots=True)
class InitResult:
    """项目接入产生的文件变化。"""

    root: Path
    created: tuple[str, ...]
    updated: tuple[str, ...]
    preserved: tuple[str, ...]


def _validate_file(path: Path, start: str | None = None, end: str | None = None) -> None:
    """写入前验证所有目标，避免产生只完成一部分的接入结果。"""

    if not path.exists():
        return
    if not path.is_file():
        raise WorkspaceError(f"目标不是普通文件：{path}")
    if start is None or end is None:
        return
    content = path.read_text(encoding="utf-8")
    start_count = content.count(start)
    end_count = content.count(end)
    if start_count != end_count:
        raise WorkspaceError(f"检测到不完整的 AI Dev OS 托管区块：{path}")
    if start_count > 1:
        raise WorkspaceError(f"检测到重复的 AI Dev OS 托管区块：{path}")
    if start_count == 1 and content.index(start) > content.index(end):
        raise WorkspaceError(f"检测到顺序无效的 AI Dev OS 托管区块：{path}")


def _validate_hooks(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise WorkspaceError(f"目标不是普通文件：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"Codex Hook 配置不是有效 JSON：{path}：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks", {}), dict):
        raise WorkspaceError(f"Codex Hook 配置必须包含对象类型 hooks：{path}")
    for event_name, groups in payload.get("hooks", {}).items():
        if not isinstance(groups, list):
            raise WorkspaceError(f"Codex Hook 配置 {event_name} 必须是数组：{path}")
        if any(
            not isinstance(group, dict)
            or not isinstance(group.get("hooks", []), list)
            or any(not isinstance(hook, dict) for hook in group.get("hooks", []))
            for group in groups
        ):
            raise WorkspaceError(f"Codex Hook 配置 {event_name} 的 hooks 结构无效：{path}")


def _ensure_hooks(path: Path) -> str:
    existed = path.exists()
    payload = json.loads(path.read_text(encoding="utf-8")) if existed else {}
    hooks = payload.setdefault("hooks", {})
    changed = False
    for event_name in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        groups = hooks.setdefault(event_name, [])
        managed_index = next(
            (
                index
                for index, group in enumerate(groups)
                if any(
                    "ai-dev-os hook" in str(hook.get("command", ""))
                    or "workspace_runtime.py" in str(hook.get("command", ""))
                    for hook in group.get("hooks", [])
                )
            ),
            None,
        )
        desired = _hook_group(event_name)
        if managed_index is None:
            groups.append(desired)
            changed = True
        elif groups[managed_index] != desired:
            groups[managed_index] = desired
            changed = True
    if not existed or changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        WorkspaceStore.write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        return "created" if not existed else "updated"
    return "preserved"


def _validate_project_config(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise WorkspaceError(f"目标不是普通文件：{path}")
    load_project_config(path.parent)


def _ensure_project_config(root: Path) -> str:
    path = root / CONFIG_NAME
    desired = initialized_project_config(root)
    if not path.exists():
        WorkspaceStore.write_text(
            path,
            json.dumps(desired, ensure_ascii=False, indent=2),
        )
        return "created"
    current = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    if "project_id" not in current:
        current["project_id"] = current.get("task_project_id") or desired["project_id"]
        changed = True
    for key in (
        "auto_execute_in_progress",
        "dispatcher_poll_seconds",
        "codex_sandbox",
        "codex_model",
    ):
        if key not in current:
            current[key] = desired[key]
            changed = True
    if "automation" not in current:
        current["automation"] = desired["automation"]
        changed = True
    else:
        current_automation = current["automation"]
        desired_automation = desired["automation"]
        for key, value in desired_automation.items():
            if key not in current_automation:
                current_automation[key] = value
                changed = True
    if changed:
        WorkspaceStore.write_text(
            path,
            json.dumps(current, ensure_ascii=False, indent=2),
        )
        return "updated"
    return "preserved"


def _append_managed_block(path: Path, block: str, start: str, end: str) -> str:
    """幂等追加已经过预检的托管区块。"""

    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return "created"

    content = path.read_text(encoding="utf-8")
    has_start = start in content
    if has_start:
        start_index = content.index(start)
        end_index = content.index(end, start_index) + len(end)
        current_block = content[start_index:end_index]
        desired_block = block.rstrip("\n")
        if current_block == desired_block:
            return "preserved"
        path.write_text(
            f"{content[:start_index]}{desired_block}{content[end_index:]}",
            encoding="utf-8",
        )
        return "updated"

    separator = (
        ""
        if not content or content.endswith("\n\n")
        else "\n"
        if content.endswith("\n")
        else "\n\n"
    )
    path.write_text(f"{content}{separator}{block}", encoding="utf-8")
    return "updated"


def _create_if_missing(path: Path, content: str) -> str:
    if path.exists():
        return "preserved"
    path.write_text(content, encoding="utf-8")
    return "created"


def _validate_user_principles_path() -> None:
    path = user_config.user_principles_path()
    if path.parent.exists() and not path.parent.is_dir():
        raise WorkspaceError(f"用户级配置路径不是目录：{path.parent}")
    _validate_file(path)


def _ensure_user_principles(project_root: Path) -> str:
    """创建唯一用户级原则；旧项目文件仅作为首次迁移来源。"""

    path = user_config.user_principles_path()
    if path.exists():
        return "preserved"
    legacy_path = project_root / user_config.USER_PRINCIPLES_NAME
    content = (
        legacy_path.read_text(encoding="utf-8")
        if legacy_path.is_file()
        else user_config.DEFAULT_USER_PRINCIPLES
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    WorkspaceStore.write_text(path, content)
    return "created"


def _ensure_gitignore(path: Path) -> str:
    """仅补充缺少的本地状态规则，不复制项目已有规则。"""

    if not path.exists():
        path.write_text(GITIGNORE_BLOCK, encoding="utf-8")
        return "created"
    content = path.read_text(encoding="utf-8")
    if GITIGNORE_START in content:
        return "preserved"
    existing = {line.strip() for line in content.splitlines()}
    missing = [entry for entry in (".workspace/", ".worktrees/") if entry not in existing]
    if not missing:
        return "preserved"
    block = f"{GITIGNORE_START}\n" + "\n".join(missing) + f"\n{GITIGNORE_END}\n"
    return _append_managed_block(path, block, GITIGNORE_START, GITIGNORE_END)


def _resolve_project_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if not resolved.exists():
        raise WorkspaceError(f"项目目录不存在：{resolved}")
    if not resolved.is_dir():
        raise WorkspaceError(f"项目路径不是目录：{resolved}")
    return resolved


def _apply_current_project_files(resolved: Path) -> InitResult:
    """预检全部目标后，将受控接入内容更新到当前版本。"""

    targets = {
        "AGENTS.md": (AGENTS_START, AGENTS_END),
        "PROJECT_INTENT.md": (None, None),
        ".gitignore": (GITIGNORE_START, GITIGNORE_END),
    }
    for name, markers in targets.items():
        _validate_file(resolved / name, *markers)
    codex_dir = resolved / ".codex"
    if codex_dir.exists() and not codex_dir.is_dir():
        raise WorkspaceError(f"目标不是目录：{codex_dir}")
    _validate_hooks(codex_dir / "hooks.json")
    _validate_project_config(resolved / CONFIG_NAME)
    _validate_user_principles_path()

    outcomes = {
        user_config.USER_PRINCIPLES_DISPLAY_PATH: _ensure_user_principles(resolved),
        "AGENTS.md": _append_managed_block(
            resolved / "AGENTS.md", AGENTS_BLOCK, AGENTS_START, AGENTS_END
        ),
        "PROJECT_INTENT.md": _create_if_missing(resolved / "PROJECT_INTENT.md", PROJECT_INTENT),
        ".gitignore": _ensure_gitignore(resolved / ".gitignore"),
        ".codex/hooks.json": _ensure_hooks(codex_dir / "hooks.json"),
        CONFIG_NAME: _ensure_project_config(resolved),
    }
    return InitResult(
        root=resolved,
        created=tuple(name for name, outcome in outcomes.items() if outcome == "created"),
        updated=tuple(name for name, outcome in outcomes.items() if outcome == "updated"),
        preserved=tuple(name for name, outcome in outcomes.items() if outcome == "preserved"),
    )


def initialize_project(root: Path) -> InitResult:
    """在不创建 Requirement Workspace 的前提下接入一个现有项目。"""

    return _apply_current_project_files(_resolve_project_root(root))


def migrate_project(root: Path) -> InitResult:
    """只在持久格式确有变化时迁移已接入项目。"""

    resolved = _resolve_project_root(root)
    if not (resolved / CONFIG_NAME).exists():
        raise WorkspaceError(
            f"项目尚未通过 ai-dev-os init 接入，无法迁移：{resolved}"
        )
    return _apply_current_project_files(resolved)
