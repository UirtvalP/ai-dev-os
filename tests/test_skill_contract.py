from pathlib import Path


def test_workspace_skill_keeps_restore_and_automatic_completion_contract() -> None:
    root = Path(__file__).parents[1]
    skill = (root / "skills" / "workspace-orchestrator" / "SKILL.md").read_text(encoding="utf-8")

    assert "workspace current" in skill
    assert "workspace resume REQ-ID" in skill
    assert "workspace bootstrap REQ-ID" in skill
    assert 'workspace bootstrap --request "用户当前开发请求"' in skill
    assert "多个活动 Task" in skill
    assert "workspace checkpoint REQ-ID" in skill
    assert "workspace handoff REQ-ID" in skill
    assert "workspace review REQ-ID" in skill
    assert "验收标准是必要条件，但不是充分条件" in skill
    assert "技术上正确" in skill
    assert "用户原则、项目意图或需求意图" in skill
    assert "`PASS`、`PARTIAL` 或 `VIOLATION`" in skill
    assert "明确以“新增需求”“新建需求”或“创建需求”发令" in skill
    assert "默认在全部门禁通过后自动完成 Requirement" in skill
    assert "只有需求明确记录人工测试或人工验收" in skill
    assert "默认自动完成路径不伪造人工批准" in skill
    assert "workspace request-changes" in skill
    assert "requirement-review" in skill
    assert "主面板可见工作卡" in skill
    assert "待推送归档记录" in skill
    assert ".ai-dev-os.json" in skill
