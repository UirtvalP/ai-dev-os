"""将现有项目安全接入 AI Dev OS。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .project_config import CONFIG_NAME, initialized_project_config, load_project_config
from .workspace import WorkspaceError, WorkspaceStore

AGENTS_START = "<!-- ai-dev-os:start -->"
AGENTS_END = "<!-- ai-dev-os:end -->"
GITIGNORE_START = "# ai-dev-os:start"
GITIGNORE_END = "# ai-dev-os:end"
HOOK_COMMAND = "ai-dev-os hook"

AGENTS_BLOCK = f"""{AGENTS_START}
## AI Dev OS

- 仓库级 Codex Hook 启用时，由 `SessionStart` / `UserPromptSubmit` 自动触发 bootstrap；
  Agent 不得重复执行内部的 Session、Requirement、Task、dashi 或 Git 步骤。
- Hook 未启用或未受信任时，只回退执行一次
  `workspace bootstrap --request "<当前开发请求>"`。请求包含明确的 `REQ-<数字>` 或 Task ID
  时，将它传给 bootstrap。
- 将 Hook 或回退命令返回的 Context Snapshot 视为当前需求、状态、交接和下一步行动的事实来源。
- 必须阅读 `USER_PRINCIPLES.md`、`PROJECT_INTENT.md` 和当前需求的 `intent.md`。
- 多个活动 Requirement 或多个 `in_progress` Task 存在歧义时，不得静默选择。
- 语义工作完成后只触发一次 `workspace finalize REQ-ID`；验证、checkpoint、Task review、
  handoff、Git changed files 和 Session detach 由 Automation Runtime 连续执行。
- 用户批准时使用 dashi 专用 Review 卡或 `workspace confirm REQ-ID --user-confirmed`；
  用户要求修改时使用 Review 卡新增留言并退回，或执行 `workspace request-changes`。
- dashi 中未绑定的普通开发 Task 被用户移到 `in_progress` 后，由本地 Dispatcher 自动启动或
  恢复 Codex；Agent 不得重复认领或再次启动执行。Requirement Review 卡不进入该路径。
{AGENTS_END}
"""

USER_PRINCIPLES = """# 用户原则

记录适用于本项目所有需求的长期工作偏好。除非当前需求明确记录合理例外，否则这些原则均为强制约束。

## 默认原则

- 优先采用能够安全完成任务的最轻工作流。
- 保留现有用户文件和人类可读的本地状态。
- 避免未经需求证实的抽象、集成与自动化。
- 技术正确但违反已记录意图的修改不算完成。
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
    if event_name != "SessionEnd":
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
    has_start = start in content
    has_end = end in content
    if has_start != has_end:
        raise WorkspaceError(f"检测到不完整的 AI Dev OS 托管区块：{path}")


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
    for event_name in ("SessionStart", "UserPromptSubmit", "SessionEnd"):
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
    for key in (
        "auto_execute_in_progress",
        "dispatcher_poll_seconds",
        "codex_sandbox",
    ):
        if key not in current:
            current[key] = desired[key]
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


def initialize_project(root: Path) -> InitResult:
    """在不创建 Requirement Workspace 的前提下接入一个现有项目。"""

    resolved = root.expanduser().resolve()
    if not resolved.exists():
        raise WorkspaceError(f"项目目录不存在：{resolved}")
    if not resolved.is_dir():
        raise WorkspaceError(f"项目路径不是目录：{resolved}")

    targets = {
        "AGENTS.md": (AGENTS_START, AGENTS_END),
        "USER_PRINCIPLES.md": (None, None),
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

    outcomes = {
        "AGENTS.md": _append_managed_block(
            resolved / "AGENTS.md", AGENTS_BLOCK, AGENTS_START, AGENTS_END
        ),
        "USER_PRINCIPLES.md": _create_if_missing(resolved / "USER_PRINCIPLES.md", USER_PRINCIPLES),
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
