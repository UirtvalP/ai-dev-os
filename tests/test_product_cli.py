from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from workspace_orchestrator import product_cli, user_config
from workspace_orchestrator.adapters.package import ToolInstallerError, ToolUpgradeResult
from workspace_orchestrator.product_cli import main
from workspace_orchestrator.project_config import default_task_project_id
from workspace_orchestrator.project_init import (
    AGENTS_END,
    AGENTS_START,
    GITIGNORE_START,
    initialize_project,
)


def test_init_onboards_existing_project_without_creating_workspace(tmp_path: Path, capsys) -> None:
    (tmp_path / "README.md").write_text("# Existing project\n", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "AI Dev OS 已接入" in output
    assert "workspace new" in output
    assert (tmp_path / "AGENTS.md").is_file()
    assert not (tmp_path / "USER_PRINCIPLES.md").exists()
    assert user_config.user_principles_path().is_file()
    assert "跨项目、长期有效" in user_config.user_principles_path().read_text(encoding="utf-8")
    assert (tmp_path / "PROJECT_INTENT.md").is_file()
    config = json.loads((tmp_path / ".ai-dev-os.json").read_text(encoding="utf-8"))
    assert config["task_provider"] == "dashi"
    assert config["task_project_id"]
    assert config["auto_execute_in_progress"] is True
    assert config["dispatcher_poll_seconds"] == 2.0
    assert config["codex_sandbox"] == "workspace-write"
    assert config["codex_model"] is None
    assert config["automation"]["auto_finish_pushed_thread"] is True
    assert GITIGNORE_START in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert not (tmp_path / ".workspace").exists()
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Existing project\n"


def test_init_preserves_existing_content_and_is_idempotent(tmp_path: Path, capsys) -> None:
    agents = tmp_path / "AGENTS.md"
    principles = user_config.user_principles_path()
    intent = tmp_path / "PROJECT_INTENT.md"
    gitignore = tmp_path / ".gitignore"
    agents.write_text("# Existing instructions\n", encoding="utf-8")
    principles.parent.mkdir(parents=True)
    principles.write_text("# My principles\n", encoding="utf-8")
    intent.write_text("# My intent\n", encoding="utf-8")
    gitignore.write_text(".env\n", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 0
    capsys.readouterr()
    first = {
        str(path.relative_to(tmp_path)): path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert main(["init", str(tmp_path)]) == 0
    second_output = capsys.readouterr().out
    second = {
        str(path.relative_to(tmp_path)): path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    assert first == second
    assert agents.read_text(encoding="utf-8").startswith("# Existing instructions\n")
    assert agents.read_text(encoding="utf-8").count(AGENTS_START) == 1
    assert principles.read_text(encoding="utf-8") == "# My principles\n"
    assert intent.read_text(encoding="utf-8") == "# My intent\n"
    assert gitignore.read_text(encoding="utf-8").startswith(".env\n")
    assert "~/.ai-dev-os/USER_PRINCIPLES.md" in second_output
    assert "已保留" in second_output


def test_init_rejects_missing_directory(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing"

    assert main(["init", str(missing)]) == 2

    assert "项目目录不存在" in capsys.readouterr().err
    assert not missing.exists()


def test_init_promotes_legacy_project_principles_once(tmp_path: Path) -> None:
    legacy = tmp_path / "USER_PRINCIPLES.md"
    legacy.write_text("# 我的跨项目原则\n", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 0

    assert user_config.user_principles_path().read_text(encoding="utf-8") == "# 我的跨项目原则\n"
    assert legacy.read_text(encoding="utf-8") == "# 我的跨项目原则\n"


def test_init_never_overwrites_global_principles_from_project(tmp_path: Path) -> None:
    global_principles = user_config.user_principles_path()
    global_principles.parent.mkdir(parents=True)
    global_principles.write_text("# 全局原则\n", encoding="utf-8")
    (tmp_path / "USER_PRINCIPLES.md").write_text("# 旧项目原则\n", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 0

    assert global_principles.read_text(encoding="utf-8") == "# 全局原则\n"


def test_init_rejects_invalid_user_config_root_before_project_writes(
    tmp_path: Path, capsys
) -> None:
    user_config.user_config_root().write_text("不是目录", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 2

    assert "用户级配置路径不是目录" in capsys.readouterr().err
    assert user_config.user_config_root().read_text(encoding="utf-8") == "不是目录"
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "PROJECT_INTENT.md").exists()


def test_init_rejects_incomplete_managed_block(tmp_path: Path, capsys) -> None:
    (tmp_path / "AGENTS.md").write_text(f"{AGENTS_START}\n", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 2

    assert "不完整的 AI Dev OS 托管区块" in capsys.readouterr().err
    assert not (tmp_path / "USER_PRINCIPLES.md").exists()
    assert not (tmp_path / "PROJECT_INTENT.md").exists()
    assert not (tmp_path / ".workspace").exists()


def test_init_rejects_unknown_task_provider_before_other_writes(tmp_path: Path, capsys) -> None:
    (tmp_path / ".ai-dev-os.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_provider": "dahsi",
                "task_project_id": "demo",
            }
        ),
        encoding="utf-8",
    )

    assert main(["init", str(tmp_path)]) == 2

    assert "不支持的 task_provider" in capsys.readouterr().err
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_upgrades_existing_project_config_with_dispatcher_defaults(
    tmp_path: Path, capsys
) -> None:
    config_path = tmp_path / ".ai-dev-os.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_provider": "dashi",
                "task_project_id": "demo",
            }
        ),
        encoding="utf-8",
    )

    assert main(["init", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert "已更新" in output
    assert config["task_project_id"] == "demo"
    assert config["auto_execute_in_progress"] is True
    assert config["dispatcher_poll_seconds"] == 2.0
    assert config["codex_sandbox"] == "workspace-write"
    assert config["codex_model"] is None
    assert config["automation"]["auto_finish_pushed_thread"] is True


def test_default_dashi_project_id_distinguishes_same_named_directories(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "project"
    second = tmp_path / "two" / "project"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    first_id = default_task_project_id(first)
    second_id = default_task_project_id(second)

    assert first_id.startswith("project-")
    assert second_id.startswith("project-")
    assert first_id != second_id


def test_init_does_not_duplicate_existing_ignore_rules(tmp_path: Path, capsys) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".workspace/\n.worktrees/\n", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 0

    capsys.readouterr()
    assert gitignore.read_text(encoding="utf-8") == ".workspace/\n.worktrees/\n"
    assert GITIGNORE_START not in gitignore.read_text(encoding="utf-8")


def test_init_updates_outdated_managed_agents_block(tmp_path: Path, capsys) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        f"# Existing\n\n{AGENTS_START}\n旧的手工 bootstrap 指引\n<!-- ai-dev-os:end -->\n",
        encoding="utf-8",
    )

    assert main(["init", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    content = agents.read_text(encoding="utf-8")
    assert "已更新：AGENTS.md" in output
    assert "全局安装的 `ai-dev-os hook`" in content
    assert "运行时契约" in content
    assert "~/.ai-dev-os/USER_PRINCIPLES.md" in content
    assert "旧的手工 bootstrap 指引" not in content


def test_installed_wheel_init_delivers_hook_without_project_source_or_venv(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    venv = tmp_path / "tool-env"
    project = tmp_path / "new-project"
    project.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, text=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(next(dist.glob("*.whl")))],
        check=True,
        capture_output=True,
        text=True,
    )
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    ai_dev_os = scripts / ("ai-dev-os.exe" if os.name == "nt" else "ai-dev-os")
    workspace = scripts / ("workspace.exe" if os.name == "nt" else "workspace")

    isolated_home = tmp_path / "user-home"
    isolated_home.mkdir()
    subprocess_env = os.environ.copy()
    subprocess_env["HOME"] = str(isolated_home)
    subprocess_env["USERPROFILE"] = str(isolated_home)
    subprocess.run(
        [str(ai_dev_os), "init", str(project)],
        check=True,
        capture_output=True,
        env=subprocess_env,
    )
    subprocess.run(
        [str(workspace), "--root", str(project), "new", "Wheel hook"],
        check=True,
        capture_output=True,
    )
    event = {
        "session_id": "wheel-thread",
        "cwd": str(project),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "继续 REQ-001",
    }
    result = subprocess.run(
        [str(ai_dev_os), "hook"],
        input=json.dumps(event),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        env=subprocess_env,
    )

    hooks = json.loads((project / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert "REQ-001" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "AI Dev OS 运行时契约" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "~/.ai-dev-os/USER_PRINCIPLES.md" in result.stdout
    assert hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "ai-dev-os hook"
    assert (isolated_home / ".ai-dev-os" / "USER_PRINCIPLES.md").is_file()
    assert not (project / "USER_PRINCIPLES.md").exists()
    assert not (project / "src").exists()
    assert not (project / ".venv").exists()
    assert shutil.which("python", path=str(project)) is None


def test_init_preserves_explicitly_disabled_auto_finish(tmp_path: Path) -> None:
    config_path = tmp_path / ".ai-dev-os.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_provider": None,
                "task_project_id": None,
                "automation": {"auto_finish_pushed_thread": False},
            }
        ),
        encoding="utf-8",
    )

    assert main(["init", str(tmp_path)]) == 0

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["automation"]["auto_finish_pushed_thread"] is False
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    stop_hook = hooks["hooks"]["Stop"][0]["hooks"][0]
    assert stop_hook["async"] is True
    assert stop_hook["timeout"] == 30


def test_global_upgrade_uses_configured_source(monkeypatch, capsys) -> None:
    calls: list[str] = []

    class FakeInstaller:
        def upgrade(self, source: str) -> ToolUpgradeResult:
            calls.append(source)
            return ToolUpgradeResult(source, "Resolved 1 package")

    monkeypatch.setattr(product_cli, "UvToolInstaller", FakeInstaller)

    assert main(["upgrade", "--source", "D:/releases/ai-dev-os.whl"]) == 0

    output = capsys.readouterr().out
    assert calls == ["D:/releases/ai-dev-os.whl"]
    assert "AI Dev OS 全局 CLI 已更新" in output
    assert "更新完成后，后续 ai-dev-os、workspace 与项目 Hook 调用将使用新版本能力" in output


def test_global_upgrade_reports_installer_failure(monkeypatch, capsys) -> None:
    class FailingInstaller:
        def upgrade(self, source: str) -> ToolUpgradeResult:
            raise ToolInstallerError(f"无法安装：{source}")

    monkeypatch.setattr(product_cli, "UvToolInstaller", FailingInstaller)

    assert main(["upgrade", "--source", "broken-source"]) == 2

    assert "无法安装：broken-source" in capsys.readouterr().err


def test_migrate_requires_an_initialized_project(tmp_path: Path, capsys) -> None:
    assert main(["migrate", str(tmp_path)]) == 2

    assert "尚未通过 ai-dev-os init 接入" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_migrate_updates_managed_content_and_preserves_user_configuration(
    tmp_path: Path, capsys
) -> None:
    initialize_project(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(
            f"{AGENTS_START}\n## AI Dev OS",
            f"{AGENTS_START}\n旧版托管说明",
        )
        + "\n# 用户补充说明\n",
        encoding="utf-8",
    )
    config_path = tmp_path / ".ai-dev-os.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    del config["dispatcher_poll_seconds"]
    config["auto_execute_in_progress"] = False
    config["custom_option"] = {"keep": True}
    config["automation"] = {
        "auto_finish_pushed_thread": False,
        "custom_automation": "keep",
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["UserPromptSubmit"].append(
        {"hooks": [{"type": "command", "command": "user-hook"}]}
    )
    hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

    assert main(["migrate", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    upgraded_agents = agents.read_text(encoding="utf-8")
    upgraded_config = json.loads(config_path.read_text(encoding="utf-8"))
    upgraded_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "AI Dev OS 项目格式已迁移" in output
    assert "已更新：AGENTS.md" in output
    assert upgraded_agents.count(AGENTS_START) == 1
    assert "旧版托管说明" not in upgraded_agents
    assert "# 用户补充说明" in upgraded_agents
    assert upgraded_config["dispatcher_poll_seconds"] == 2.0
    assert upgraded_config["auto_execute_in_progress"] is False
    assert upgraded_config["automation"]["auto_finish_pushed_thread"] is False
    assert upgraded_config["automation"]["custom_automation"] == "keep"
    assert upgraded_config["custom_option"] == {"keep": True}
    assert upgraded_hooks["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"] == "user-hook"

    first = {
        str(path.relative_to(tmp_path)): path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert main(["migrate", str(tmp_path)]) == 0
    second_output = capsys.readouterr().out
    second = {
        str(path.relative_to(tmp_path)): path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert first == second
    assert "已保留" in second_output
    assert "AGENTS.md" in second_output


def test_migrate_preflight_failure_leaves_all_targets_unchanged(
    tmp_path: Path, capsys
) -> None:
    config_path = tmp_path / ".ai-dev-os.json"
    original_config = {
        "schema_version": 1,
        "task_provider": None,
        "task_project_id": None,
        "custom_option": "keep",
    }
    config_path.write_text(json.dumps(original_config), encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    agents.write_text(f"# 用户内容\n\n{AGENTS_START}\n", encoding="utf-8")

    assert main(["migrate", str(tmp_path)]) == 2

    assert "不完整的 AI Dev OS 托管区块" in capsys.readouterr().err
    assert json.loads(config_path.read_text(encoding="utf-8")) == original_config
    assert agents.read_text(encoding="utf-8") == f"# 用户内容\n\n{AGENTS_START}\n"
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / "USER_PRINCIPLES.md").exists()
    assert AGENTS_END not in agents.read_text(encoding="utf-8")


def test_migrate_adds_missing_nested_defaults_without_replacing_unknown_fields(
    tmp_path: Path,
) -> None:
    initialize_project(tmp_path)
    config_path = tmp_path / ".ai-dev-os.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["automation"] = {"custom_automation": "keep"}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert main(["migrate", str(tmp_path)]) == 0

    upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    assert upgraded["automation"] == {
        "custom_automation": "keep",
        "auto_finish_pushed_thread": True,
    }
