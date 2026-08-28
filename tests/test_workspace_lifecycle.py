import json
from pathlib import Path

import pytest

from workspace_orchestrator.cli import main
from workspace_orchestrator.context import build_snapshot, checkpoint, handoff
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def test_create_initializes_complete_human_readable_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)

    first = store.create("Add authentication", acceptance=["Valid users can log in"])
    second = store.create("Add audit log")

    assert first == "REQ-001"
    assert second == "REQ-002"
    data = store.load(first)
    assert data["meta"]["id"] == first
    assert data["meta"]["title"] == "Add authentication"
    assert "- [ ] Valid users can log in" in data["requirement"]
    assert data["sessions"] == []
    assert {path.name for path in data["path"].iterdir()} == {
        "meta.json",
        "requirement.md",
        "state.md",
        "plan.md",
        "decisions.md",
        "verification.md",
        "handoff.md",
        "sessions.json",
    }


def test_load_rejects_incomplete_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    path = store.path_for("REQ-001")
    path.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="incomplete"):
        store.load("REQ-001")


def test_current_id_resolves_only_active_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Only active requirement")

    assert store.current_id() == requirement_id


def test_current_id_refuses_to_guess_between_active_workspaces(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    store.create("First")
    store.create("Second")

    with pytest.raises(WorkspaceError, match="Multiple active"):
        store.current_id()


def test_checkpoint_and_handoff_restore_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("Add authentication", goal="Implement JWT authentication")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")

    checkpoint(
        store,
        requirement_id,
        phase="implementation",
        completed=["Login endpoint"],
        next_action="Implement middleware",
        verification="pytest tests/auth: PASS",
    )
    handoff(
        store,
        requirement_id,
        completed=["Login endpoint"],
        files_changed=["src/auth/login.py"],
        current_state="Login works; middleware is pending.",
        important_context="Keep auth logic in the auth module.",
        next_action="Implement middleware",
    )

    monkeypatch.setenv("CODEX_THREAD_ID", "thread-b")
    snapshot = build_snapshot(store, requirement_id)
    checkpoint(store, requirement_id)
    data = store.load(requirement_id)

    assert "REQ-001 Add authentication" in snapshot
    assert "Implement JWT authentication" in snapshot
    assert "- Login endpoint" in snapshot
    assert "Login works; middleware is pending." in snapshot
    assert "Implement middleware" in snapshot
    assert data["meta"]["status"] == "in_progress"
    assert [session["id"] for session in data["sessions"]] == ["thread-a", "thread-b"]
    assert data["sessions"][0]["result"] == "completed"


def test_cli_lifecycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root_args = ["--root", str(tmp_path)]
    assert main([*root_args, "new", "CLI demo", "--acceptance", "Snapshot is concise"]) == 0
    assert "Created REQ-001" in capsys.readouterr().out

    assert main([*root_args, "checkpoint", "REQ-001", "--phase", "implementation"]) == 0
    assert main([*root_args, "resume", "REQ-001"]) == 0
    output = capsys.readouterr().out
    assert "# Workspace Context" in output
    assert "Current Phase:\nimplementation" in output

    assert main([*root_args, "status", "REQ-001"]) == 0
    assert "Status: in_progress" in capsys.readouterr().out

    assert main([*root_args, "current"]) == 0
    assert capsys.readouterr().out.strip() == "REQ-001"
    assert main([*root_args, "resume"]) == 0
    assert "REQ-001 CLI demo" in capsys.readouterr().out


def test_persisted_json_is_utf8_and_readable(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create("中文需求")
    meta_path = store.path_for(requirement_id) / "meta.json"

    assert json.loads(meta_path.read_text(encoding="utf-8"))["title"] == "中文需求"


def test_cli_can_configure_dashi_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(
        [
            "--root",
            str(tmp_path),
            "new",
            "Integrated task",
            "--task-provider",
            "dashi",
            "--task-project",
            "ai-dev-os",
        ]
    )

    assert result == 0
    capsys.readouterr()
    meta = WorkspaceStore(tmp_path).load("REQ-001")["meta"]
    assert meta["task_provider"] == "dashi"
    assert meta["task_project_id"] == "ai-dev-os"
