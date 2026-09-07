"""已安装 wheel 提供的 Codex lifecycle Hook 入口。"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .adapters.agent import CodexAgentProvider
from .automation.dispatcher import start_dispatcher
from .automation.requirement_attach import AutomationAmbiguity, discover_project_root
from .automation.runtime import AutomationRuntime
from .automation.session_runtime import end_session
from .automation.task_attach import configured_task_provider
from .runtime_contract import hook_context
from .workspace import WorkspaceError, WorkspaceStore, now_iso


def _emit(event_name: str, context: str, *, system_message: str | None = None) -> None:
    payload: dict[str, object] = {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        },
    }
    if system_message:
        payload["systemMessage"] = system_message
    print(json.dumps(payload, ensure_ascii=False))


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def _continue(*, system_message: str | None = None) -> None:
    payload: dict[str, object] = {"continue": True}
    if system_message:
        payload["systemMessage"] = system_message
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    event = json.load(sys.stdin)
    execution_root = Path(str(event.get("cwd") or Path.cwd())).resolve()
    root = discover_project_root(execution_root)
    event_name = str(event.get("hook_event_name") or "")
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return 0
    store = WorkspaceStore(root, execution_root=execution_root)
    if os.environ.get("AI_DEV_OS_DISPATCHER_CHILD") != "1":
        try:
            start_dispatcher(store)
        except (OSError, WorkspaceError):
            # Dispatcher 启动失败不能阻断当前前台 Hook；status 命令提供显式诊断入口。
            pass
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": session_id})
    runtime = AutomationRuntime(store, agent)

    if event_name == "Stop":
        try:
            result = runtime.auto_finish_pushed_thread()
        except WorkspaceError as exc:
            _continue(system_message=f"AI Dev OS 自动收尾失败：{exc}")
        else:
            _continue(
                system_message=(
                    f"AI Dev OS 已自动完成 {', '.join(result.task_ids)} 并归档当前 Thread"
                    if result.completed
                    else None
                )
            )
        return 0

    if event_name == "SessionEnd":
        attached = store.attached_requirement_id(session_id)
        if attached:
            provider = configured_task_provider(store.load(attached)["meta"], store.project_root)
            end_session(store, attached, session_id, task_provider=provider)
        return 0

    prompt = str(event.get("prompt") or "")
    requirement_match = re.search(r"(?<![A-Z0-9])REQ-\d+(?![A-Z0-9])", prompt, re.IGNORECASE)
    task_ids = tuple(
        dict.fromkeys(
            match.upper()
            for match in re.findall(
                r"(?<![A-Z0-9])(?:TASK|AID)-\d+(?![A-Z0-9])", prompt, re.IGNORECASE
            )
        )
    )
    try:
        turn_id = str(event.get("turn_id") or event.get("prompt_id") or "").strip()
        snapshot = runtime.bootstrap(
            requirement_match.group(0).upper() if requirement_match else None,
            task_ids=task_ids,
            development_request=prompt or None,
            creation_key=f"{session_id}:{turn_id}" if turn_id else None,
        )
    except AutomationAmbiguity as exc:
        if event_name == "UserPromptSubmit":
            _block(str(exc))
        else:
            _emit(event_name, str(exc), system_message="Workspace 需要用户明确选择")
        return 0
    except WorkspaceError as exc:
        if event_name == "SessionStart" and "没有可恢复" in str(exc):
            return 0
        # Provider 离线已在 Runtime 内降级；这里只处理真正的本地/歧义错误。
        if event_name == "UserPromptSubmit":
            _block(str(exc))
        else:
            _emit(event_name, str(exc), system_message="Workspace 自动恢复未完成")
        return 0
    _emit(event_name, hook_context(snapshot))
    return 0


def _import_codex_thread() -> int:
    """可选兼容 Hook：只导入明确指向 Requirement 的 Codex Thread。"""

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        raise TypeError("Codex Hook 输入必须是 JSON 对象")
    session_id = str(event.get("session_id") or "").strip()
    prompt = str(event.get("prompt") or "")
    match = re.search(r"(?<![A-Z0-9])REQ-\d+(?![A-Z0-9])", prompt, re.IGNORECASE)
    if not session_id or match is None:
        _continue()
        return 0
    execution_root = Path(str(event.get("cwd") or Path.cwd())).resolve()
    root = discover_project_root(execution_root)
    requirement_id = match.group(0).upper()
    store = WorkspaceStore(root, execution_root=execution_root)
    from .executions import ExecutionStore

    executions = ExecutionStore(store)
    execution = executions.create(
        requirement_id, "NATIVE-CODEX", role="external", runtime_id="codex",
        provider="codex", prompt=prompt or "显式导入原生 Codex Thread",
        workspace_path=execution_root, source="optional-codex-hook",
        creation_key=f"optional-codex-thread:{requirement_id}:{session_id}",
        execution_policy={"mode": "import-only"},
    )
    if execution.status == "queued":
        execution = executions.update(
            execution.id, status="running", session_id=session_id,
            started_at=now_iso(), last_progress_at=now_iso(),
            summary="已显式导入原生 Codex Thread；未接管其生命周期",
        )
    elif execution.session_id != session_id:
        raise WorkspaceError("可选 Codex 导入身份冲突")
    _continue(system_message=f"AI Dev OS 已导入 {execution.id}，未接管当前 Codex 生命周期")
    return 0


def import_codex_thread_main() -> int:
    """显式兼容集成必须 fail-open，不能中断原生 Codex 使用。"""

    try:
        return _import_codex_thread()
    except (OSError, TypeError, ValueError, UnicodeError, WorkspaceError) as exc:
        _continue(system_message=f"AI Dev OS 可选 Thread 导入已跳过：{exc}")
        return 0
