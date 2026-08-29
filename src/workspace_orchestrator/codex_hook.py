"""Codex lifecycle Hook 入口；仅解析事件并触发 Automation Runtime。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _project_root(cwd: str) -> Path:
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".workspace").is_dir():
            return candidate
    return current


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


def main() -> int:
    from workspace_orchestrator.adapters.agent import CodexAgentProvider
    from workspace_orchestrator.automation.requirement_attach import AutomationAmbiguity
    from workspace_orchestrator.automation.runtime import AutomationRuntime
    from workspace_orchestrator.automation.session_runtime import end_session
    from workspace_orchestrator.automation.task_attach import configured_task_provider
    from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    event = json.load(sys.stdin)
    root = _project_root(str(event.get("cwd") or Path.cwd()))
    event_name = str(event.get("hook_event_name") or "")
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return 0
    store = WorkspaceStore(root)
    agent = CodexAgentProvider(environ={"CODEX_THREAD_ID": session_id})
    runtime = AutomationRuntime(store, agent)

    if event_name == "SessionEnd":
        attached = store.attached_requirement_id(session_id)
        if attached:
            provider = configured_task_provider(store.load(attached)["meta"])
            end_session(store, attached, session_id, task_provider=provider)
        return 0

    prompt = str(event.get("prompt") or "")
    requirement_match = re.search(
        r"(?<![A-Z0-9])REQ-\d+(?![A-Z0-9])", prompt, re.IGNORECASE
    )
    task_ids = tuple(
        dict.fromkeys(
            match.upper()
            for match in re.findall(
                r"(?<![A-Z0-9])(?:TASK|AID)-\d+(?![A-Z0-9])", prompt, re.IGNORECASE
            )
        )
    )
    try:
        snapshot = runtime.bootstrap(
            requirement_match.group(0).upper() if requirement_match else None,
            task_ids=task_ids,
            development_request=prompt or None,
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
        if event_name == "UserPromptSubmit":
            _block(str(exc))
        else:
            _emit(event_name, str(exc), system_message="Workspace 自动恢复未完成")
        return 0
    _emit(event_name, snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
