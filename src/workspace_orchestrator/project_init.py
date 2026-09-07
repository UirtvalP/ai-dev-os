"""将现有项目安全接入 AI Dev OS。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import user_config
from .project_config import CONFIG_NAME, initialized_project_config, load_project_config
from .workspace import WorkspaceError, WorkspaceStore

AGENTS_START = "<!-- ai-dev-os:start -->"
AGENTS_END = "<!-- ai-dev-os:end -->"
GITIGNORE_START = "# ai-dev-os:start"
GITIGNORE_END = "# ai-dev-os:end"
HOOK_COMMAND = "ai-dev-os hook"
OPTIONAL_CODEX_HOOK_COMMAND = "ai-dev-os hook import-codex-thread"
_LEGACY_MODULE_COMMAND = re.compile(
    r'(?i)(?:"[^"]*python(?:\.exe)?"|\S*python(?:\.exe)?)\s+'
    r'(?:-m\s+workspace_orchestrator\.(?:codex_hook|hook_runtime)'
    r'|"?[^";&|]*workspace_runtime\.py"?)\Z'
)

AGENTS_BLOCK = f"""{AGENTS_START}
## AI Dev OS

- `.codex/hooks.json` 调用全局安装的 `ai-dev-os hook`，更新一次 CLI 后运行时能力即全局生效。
- Hook 动态注入的“AI Dev OS 运行时契约”和 Context Snapshot 是当前流程与状态的事实来源；
  不要复制或固化特定版本的运行时步骤。
- Hook 未启用或未受信任时，只回退执行一次 `workspace bootstrap --request "<当前开发请求>"`；
  请求包含明确的 `REQ-<数字>` 或 Task ID 时原样传入。
- 必须阅读用户级 `~/.ai-dev-os/USER_PRINCIPLES.md`、项目级 `PROJECT_INTENT.md` 和当前需求的 `intent.md`。
- 明确“新增/新建/创建需求”时由 Runtime 幂等创建并接入，不再二次确认；普通修改遇到多个活动 Requirement 或多个 `in_progress` Task 时不得静默选择。
- 语义工作完成后只触发一次 `workspace finalize REQ-ID`。默认在已知验证、验收标准与 Intent 门禁通过后自动完成 Requirement；只有明确要求人工测试或验收时才进入人工 Review。
- finalize 后的待推送记录由 Stop Hook 在新提交完整推送后完成关联 Task 并归档 Thread。
- 每个活动 Requirement 必须在任务面板保持至少一张可见工作卡；Provider 离线或并发重试由 Runtime 幂等补偿。
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


def _is_managed_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    return any(_is_managed_command(str(hook.get(field, ""))) for field in (
        "command", "commandWindows",
    ))


def _is_managed_command(command: str) -> bool:
    normalized = command.strip()
    return normalized in {HOOK_COMMAND, OPTIONAL_CODEX_HOOK_COMMAND} or bool(
        _LEGACY_MODULE_COMMAND.fullmatch(normalized)
    )


def _hook_commands(payload: dict[str, Any]) -> list[str]:
    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        return []
    return [
        str(hook.get(field, ""))
        for event_groups in hooks.values()
        for group in event_groups
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
        for field in ("command", "commandWindows")
        if hook.get(field)
    ]


def _remove_managed_hooks(path: Path) -> str:
    """只移除 AI Dev OS Hook，保留用户和其他工具的 Hook。"""

    if not path.exists():
        return "preserved"
    _validate_hooks(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    hooks = payload.get("hooks", {})
    changed = False
    for event_name in tuple(hooks):
        filtered_groups: list[dict[str, object]] = []
        for group in hooks[event_name]:
            retained = [hook for hook in group.get("hooks", []) if not _is_managed_hook(hook)]
            if len(retained) != len(group.get("hooks", [])):
                changed = True
            if retained:
                filtered_groups.append({**group, "hooks": retained})
            elif group.get("hooks"):
                changed = True
        if filtered_groups:
            hooks[event_name] = filtered_groups
        else:
            hooks.pop(event_name, None)
    if not changed:
        return "preserved"
    if not hooks and set(payload) == {"hooks"}:
        path.unlink()
        return "removed"
    WorkspaceStore.write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return "updated"


def enable_codex_hooks(root: Path) -> str:
    """显式启用只导入原生 Codex Thread 的兼容 Hook。"""

    resolved = _resolve_project_root(root)
    path = resolved / ".codex" / "hooks.json"
    _validate_hooks(path)
    existed = path.exists()
    payload = json.loads(path.read_text(encoding="utf-8")) if existed else {"hooks": {}}
    hooks = payload.setdefault("hooks", {})
    groups = hooks.setdefault("UserPromptSubmit", [])
    commands = _hook_commands(payload)
    desired = {
        "hooks": [{
            "type": "command",
            "command": OPTIONAL_CODEX_HOOK_COMMAND,
            "commandWindows": OPTIONAL_CODEX_HOOK_COMMAND,
            "statusMessage": "可选导入当前 Codex Thread",
        }]
    }
    if desired in groups and not any(
        _is_managed_command(command) and command.strip() != OPTIONAL_CODEX_HOOK_COMMAND
        for command in commands
    ):
        return "preserved"
    if any(_is_managed_command(command) for command in commands):
        # 清除旧生命周期组，避免“启用导入”意外保留 bootstrap/finalize 行为。
        _remove_managed_hooks(path)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"hooks": {}}
        hooks = payload.setdefault("hooks", {})
        groups = hooks.setdefault("UserPromptSubmit", [])
    groups.append(desired)
    path.parent.mkdir(parents=True, exist_ok=True)
    WorkspaceStore.write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return "created" if not existed else "updated"


def disable_codex_hooks(root: Path) -> str:
    """显式关闭所有 AI Dev OS Codex Hook，不触碰其他 Hook。"""

    return _remove_managed_hooks(_resolve_project_root(root) / ".codex" / "hooks.json")


def codex_hooks_status(root: Path) -> dict[str, object]:
    resolved = _resolve_project_root(root)
    path = resolved / ".codex" / "hooks.json"
    enabled = False
    legacy = False
    if path.is_file():
        _validate_hooks(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        commands = _hook_commands(payload)
        enabled = OPTIONAL_CODEX_HOOK_COMMAND in commands
        legacy = any(
            _is_managed_command(command)
            and command.strip() != OPTIONAL_CODEX_HOOK_COMMAND
            for command in commands
        )
    return {"enabled": enabled, "legacy_lifecycle_present": legacy, "path": str(path)}


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
    if not isinstance(current, dict):
        raise WorkspaceError(f"项目配置必须是 JSON 对象：{path}")
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
        if not isinstance(current_automation, dict) or not isinstance(desired_automation, dict):
            raise WorkspaceError(f"项目配置 automation 必须是 JSON 对象：{path}")
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


def _remove_managed_block(path: Path, start: str, end: str) -> str:
    if not path.exists():
        return "preserved"
    _validate_file(path, start, end)
    content = path.read_bytes()
    start_marker = start.encode("utf-8")
    end_marker = end.encode("utf-8")
    if start_marker not in content:
        return "preserved"
    start_index = content.index(start_marker)
    end_index = content.index(end_marker, start_index) + len(end_marker)
    retained = content[:start_index] + content[end_index:]
    if not retained.decode("utf-8").strip():
        path.unlink()
        return "removed"
    # 托管区块外属于用户的 AGENTS 内容必须逐字节保留，包括 CRLF 与尾空格。
    path.write_bytes(retained)
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
    """注册独立 Workbench 项目；默认不改变任何原生 Agent 行为。"""

    return register_project(root)


def register_project(root: Path) -> InitResult:
    """注册独立 Workbench 项目，不接管任何原生 Agent 生命周期。"""

    resolved = _resolve_project_root(root)
    targets = {
        "PROJECT_INTENT.md": (None, None),
        ".gitignore": (GITIGNORE_START, GITIGNORE_END),
    }
    for name, markers in targets.items():
        _validate_file(resolved / name, *markers)
    _validate_project_config(resolved / CONFIG_NAME)
    _validate_user_principles_path()

    workspace_root = resolved / ".workspace"
    if workspace_root.exists() and not workspace_root.is_dir():
        raise WorkspaceError(f"Requirement 存储路径不是目录：{workspace_root}")
    workspace_outcome = "preserved" if workspace_root.exists() else "created"
    workspace_root.mkdir(parents=True, exist_ok=True)
    outcomes = {
        user_config.USER_PRINCIPLES_DISPLAY_PATH: _ensure_user_principles(resolved),
        "PROJECT_INTENT.md": _create_if_missing(resolved / "PROJECT_INTENT.md", PROJECT_INTENT),
        ".gitignore": _ensure_gitignore(resolved / ".gitignore"),
        CONFIG_NAME: _ensure_project_config(resolved),
        ".workspace/": workspace_outcome,
    }
    return InitResult(
        root=resolved,
        created=tuple(name for name, outcome in outcomes.items() if outcome == "created"),
        updated=tuple(name for name, outcome in outcomes.items() if outcome == "updated"),
        preserved=tuple(name for name, outcome in outcomes.items() if outcome == "preserved"),
    )


def migrate_project(root: Path) -> InitResult:
    """迁移旧接入面并无损映射 Session；不删除 Requirement 事实。"""

    resolved = _resolve_project_root(root)
    if not (resolved / CONFIG_NAME).exists():
        raise WorkspaceError(
            f"项目尚未通过 ai-dev-os init 接入，无法迁移：{resolved}"
        )
    # 所有会修改的入口先统一预检，避免清理旧接入面前先写入其他目标。
    _validate_file(resolved / "AGENTS.md", AGENTS_START, AGENTS_END)
    _validate_hooks(resolved / ".codex" / "hooks.json")
    _validate_project_config(resolved / CONFIG_NAME)
    _validate_user_principles_path()
    from .executions import ExecutionService, ExecutionStore

    preflight_store = WorkspaceStore(resolved, execution_root=resolved)
    for requirement_id in preflight_store.requirement_ids():
        data = preflight_store.load(requirement_id)
        sessions = data.get("sessions")
        if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
            raise WorkspaceError(f"{requirement_id} sessions.json 必须是对象数组")
        ExecutionStore(preflight_store).list(requirement_id)
    result = register_project(resolved)
    outcomes = {
        "AGENTS.md legacy managed block": _remove_managed_block(
            resolved / "AGENTS.md", AGENTS_START, AGENTS_END,
        ),
        ".codex/hooks.json legacy lifecycle": _remove_managed_hooks(
            resolved / ".codex" / "hooks.json",
        ),
    }
    from .agent_runtime.events import RuntimeEventStore

    store = WorkspaceStore(resolved, execution_root=resolved)
    service = ExecutionService(
        ExecutionStore(store), RuntimeEventStore(store.root / "runtime-events"),
    )
    mapped_before = sum(
        1
        for requirement_id in store.requirement_ids()
        for execution in ExecutionStore(store).list(requirement_id)
        if execution.source == "legacy-thread-binding"
    )
    mapped = 0
    for requirement_id in store.requirement_ids():
        mapped += len(service.map_legacy_sessions(requirement_id))
    mapped_created = max(0, mapped - mapped_before)
    migration_name = f"legacy sessions -> executions ({mapped})"
    return InitResult(
        root=resolved,
        created=result.created,
        updated=result.updated + tuple(
            name for name, outcome in outcomes.items() if outcome in {"updated", "removed"}
        ) + ((migration_name,) if mapped_created else ()),
        preserved=result.preserved + tuple(
            name for name, outcome in outcomes.items() if outcome == "preserved"
        ) + ((migration_name,) if mapped and not mapped_created else ()),
    )
