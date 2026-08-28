from pathlib import Path


def test_workspace_skill_keeps_restore_and_approval_contract() -> None:
    root = Path(__file__).parents[1]
    skill = (root / "skills" / "workspace-orchestrator" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "workspace current" in skill
    assert "workspace resume REQ-ID" in skill
    assert "workspace bootstrap REQ-ID" in skill
    assert "workspace bootstrap`" in skill
    assert "workspace checkpoint REQ-ID" in skill
    assert "workspace handoff REQ-ID" in skill
    assert "workspace review REQ-ID" in skill
    assert "验收标准是必要条件，但不是充分条件" in skill
    assert "技术上正确" in skill
    assert "用户原则、项目意图或需求意图" in skill
    assert "`PASS`、`PARTIAL` 或 `VIOLATION`" in skill
    assert "未经用户明确批准" in skill
