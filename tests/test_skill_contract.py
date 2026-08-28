from pathlib import Path


def test_workspace_skill_keeps_restore_and_approval_contract() -> None:
    root = Path(__file__).parents[1]
    skill = (root / "skills" / "workspace-orchestrator" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "workspace current" in skill
    assert "workspace resume REQ-ID" in skill
    assert "workspace checkpoint REQ-ID" in skill
    assert "workspace handoff REQ-ID" in skill
    assert "workspace review REQ-ID" in skill
    assert "Never mark a Requirement or Dashi Issue `done` without explicit user" in skill
