"""新项目接入不得影响 Codex、Claude 或 Cursor 原生使用。"""

from __future__ import annotations

import json
from pathlib import Path

from workspace_orchestrator.native_isolation import (
    audit_native_isolation,
    require_native_isolation,
)
from workspace_orchestrator.product_cli import main
from workspace_orchestrator.project_init import initialize_project, register_project
from workspace_orchestrator.workspace import WorkspaceError


def test_project_add_preserves_native_agent_files_and_passes_isolation(
    tmp_path: Path, capsys,
) -> None:
    native_files = {
        "AGENTS.md": "# User native instructions\n",
        ".codex/hooks.json": json.dumps({"hooks": {"Stop": []}}),
        ".claude/settings.json": json.dumps({"permissions": {"allow": ["Read"]}}),
        ".cursor/rules/native.mdc": "---\nalwaysApply: true\n---\nUser native rule\n",
    }
    for relative, content in native_files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    before = {name: (tmp_path / name).read_bytes() for name in native_files}

    register_project(tmp_path)
    report = require_native_isolation(tmp_path)
    assert main(["project", "isolation-check", str(tmp_path)]) == 0
    assert '"isolated": true' in capsys.readouterr().out

    assert report.isolated and report.violations == ()
    assert before == {name: (tmp_path / name).read_bytes() for name in native_files}
    assert not any((tmp_path / ".workspace").iterdir())


def test_legacy_hook_project_fails_isolation_until_p10_migration(tmp_path: Path) -> None:
    initialize_project(tmp_path)
    report = audit_native_isolation(tmp_path)
    assert not report.isolated
    assert any("AGENTS.md" in item for item in report.violations)
    assert any(".codex/hooks.json" in item for item in report.violations)

    try:
        require_native_isolation(tmp_path)
    except WorkspaceError as exc:
        assert "Native Isolation 失败" in str(exc)
    else:
        raise AssertionError("legacy lifecycle integration 必须 fail closed")


def test_isolation_detects_claude_and_cursor_managed_commands(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"command": "ai-dev-os hook"}]}}),
        encoding="utf-8",
    )
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    (tmp_path / ".cursor" / "rules" / "managed.mdc").write_text(
        "always run workspace bootstrap", encoding="utf-8",
    )
    (tmp_path / "CLAUDE.md").write_text("run workspace bootstrap", encoding="utf-8")
    report = audit_native_isolation(tmp_path)
    assert {item.split(" 包含")[0] for item in report.violations} == {
        "CLAUDE.md", ".claude/settings.json", ".cursor/rules/managed.mdc",
    }


def test_isolation_detects_historical_codex_hook_command(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{
            "hooks": [{
                "type": "command",
                "command": "python -m workspace_orchestrator.codex_hook",
            }],
        }]},
    }), encoding="utf-8")
    report = audit_native_isolation(tmp_path)
    assert not report.isolated
    assert report.violations == (
        ".codex/hooks.json 包含 AI Dev OS 生命周期命令",
    )
