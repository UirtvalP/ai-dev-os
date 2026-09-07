from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from workspace_orchestrator.executions import ExecutionStore
from workspace_orchestrator.native_isolation import audit_native_isolation
from workspace_orchestrator.product_cli import main
from workspace_orchestrator.project_init import (
    AGENTS_END,
    AGENTS_START,
    _apply_current_project_files,
)
from workspace_orchestrator.workspace import WorkspaceStore


def test_codex_hooks_are_explicit_import_only_and_reversible(
    tmp_path: Path, capsys,
) -> None:
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user-hook"}]}]},
    }), encoding="utf-8")
    assert main(["init", str(tmp_path)]) == 0
    capsys.readouterr()
    original = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert original["hooks"]["Stop"][0]["hooks"][0]["command"] == "user-hook"

    assert main(["integration", "enable", "codex-hooks", "--root", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["integration", "enable", "codex-hooks", "--root", str(tmp_path)]) == 0
    enabled = json.loads(hooks_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for groups in enabled["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert commands.count("ai-dev-os hook import-codex-thread") == 1
    assert "ai-dev-os hook" not in commands
    assert "user-hook" in commands

    assert main(["integration", "disable", "codex-hooks", "--root", str(tmp_path)]) == 0
    capsys.readouterr()
    disabled = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert disabled["hooks"]["Stop"][0]["hooks"][0]["command"] == "user-hook"
    assert audit_native_isolation(tmp_path).isolated


def test_optional_codex_hook_imports_execution_without_bootstrap_or_session_binding(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    assert main(["init", str(tmp_path)]) == 0
    capsys.readouterr()
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Native import", task_provider=None)
    event = {
        "session_id": "native-session",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "prompt": f"把当前讨论导入 {requirement_id}",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["hook", "import-codex-thread"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["continue"] is True
    execution = ExecutionStore(store).list(requirement_id)[0]
    assert execution.source == "optional-codex-hook"
    assert execution.session_id == "native-session"
    assert execution.execution_policy == {"mode": "import-only"}
    assert store.load(requirement_id)["sessions"] == []


def test_optional_codex_hook_non_object_input_is_fail_open(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("[]"))
    assert main(["hook", "import-codex-thread"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["continue"] is True
    assert "已跳过" in output["systemMessage"]


def test_migrate_preserves_requirement_files_and_maps_legacy_sessions(tmp_path: Path) -> None:
    _apply_current_project_files(tmp_path)
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Preserve migration", task_provider=None)
    sessions_path = store.path_for(requirement_id) / "sessions.json"
    store.write_json(sessions_path, [{
        "id": "legacy-session", "agent": "codex", "result": "in_progress",
        "task_ids": ["TASK-LEGACY"], "started_at": "2026-09-07T00:00:00+00:00",
    }])
    requirement_root = store.path_for(requirement_id)
    before = {
        path.relative_to(requirement_root).as_posix(): path.read_bytes()
        for path in requirement_root.rglob("*") if path.is_file()
    }

    assert main(["migrate", str(tmp_path)]) == 0
    after = {
        name: (requirement_root / name).read_bytes()
        for name in before
    }
    assert after == before
    execution = ExecutionStore(store).list(requirement_id)[0]
    assert execution.source == "legacy-thread-binding"
    assert execution.session_id == "legacy-session"
    assert execution.task_id == "TASK-LEGACY"
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_migrate_preserves_user_wrapper_and_agents_bytes_outside_managed_block(
    tmp_path: Path,
) -> None:
    _apply_current_project_files(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    managed = agents_path.read_bytes().replace(b"\n", b"\r\n")
    customized = "    用户首行\r\n".encode() + managed + "  用户末行  \r\n".encode()
    agents_path.write_bytes(customized)
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    wrapper = 'powershell -Command "ai-dev-os hook; Invoke-UserCleanup"'
    hooks["hooks"]["Stop"].append({
        "hooks": [{"type": "command", "command": wrapper}],
    })
    hooks_path.write_text(json.dumps(hooks), encoding="utf-8")
    start_marker, end_marker = AGENTS_START.encode(), AGENTS_END.encode()
    expected_agents = customized[:customized.index(start_marker)] + customized[
        customized.index(end_marker) + len(end_marker):
    ]

    assert main(["migrate", str(tmp_path)]) == 0
    assert agents_path.read_bytes() == expected_agents
    migrated = json.loads(hooks_path.read_text(encoding="utf-8"))
    remaining = [
        hook["command"]
        for groups in migrated["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        if "command" in hook
    ]
    assert remaining == [wrapper]


def test_migrate_validates_sessions_before_removing_legacy_surfaces(
    tmp_path: Path, capsys,
) -> None:
    _apply_current_project_files(tmp_path)
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Invalid sessions", task_provider=None)
    store.write_json(store.path_for(requirement_id) / "sessions.json", {"not": "a list"})
    agents = (tmp_path / "AGENTS.md").read_bytes()
    hooks = (tmp_path / ".codex" / "hooks.json").read_bytes()

    assert main(["migrate", str(tmp_path)]) == 2
    assert "sessions.json 必须是对象数组" in capsys.readouterr().err
    assert (tmp_path / "AGENTS.md").read_bytes() == agents
    assert (tmp_path / ".codex" / "hooks.json").read_bytes() == hooks


@pytest.mark.parametrize(
    ("legacy_result", "execution_status"),
    [
        ("in_progress", "running"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("blocked", "blocked"),
        ("pending_auto_finish", "waiting"),
        ("detached", "waiting"),
        ("future-state", "waiting"),
    ],
)
def test_legacy_session_result_mapping_is_not_falsely_completed(
    tmp_path: Path, legacy_result: str, execution_status: str,
) -> None:
    assert main(["init", str(tmp_path)]) == 0
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Legacy state", task_provider=None)
    store.write_json(store.path_for(requirement_id) / "sessions.json", [{
        "id": "legacy", "agent": "codex", "result": legacy_result, "task_ids": ["TASK"],
    }])
    assert main(["migrate", str(tmp_path)]) == 0
    assert ExecutionStore(store).list(requirement_id)[0].status == execution_status
