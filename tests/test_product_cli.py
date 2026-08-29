from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from workspace_orchestrator.product_cli import main
from workspace_orchestrator.project_config import default_task_project_id
from workspace_orchestrator.project_init import AGENTS_START, GITIGNORE_START


def test_init_onboards_existing_project_without_creating_workspace(tmp_path: Path, capsys) -> None:
    (tmp_path / "README.md").write_text("# Existing project\n", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "AI Dev OS 已接入" in output
    assert "workspace new" in output
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "USER_PRINCIPLES.md").is_file()
    assert (tmp_path / "PROJECT_INTENT.md").is_file()
    config = json.loads((tmp_path / ".ai-dev-os.json").read_text(encoding="utf-8"))
    assert config["task_provider"] == "dashi"
    assert config["task_project_id"]
    assert GITIGNORE_START in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert not (tmp_path / ".workspace").exists()
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Existing project\n"


def test_init_preserves_existing_content_and_is_idempotent(tmp_path: Path, capsys) -> None:
    agents = tmp_path / "AGENTS.md"
    principles = tmp_path / "USER_PRINCIPLES.md"
    intent = tmp_path / "PROJECT_INTENT.md"
    gitignore = tmp_path / ".gitignore"
    agents.write_text("# Existing instructions\n", encoding="utf-8")
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
    assert "已保留：AGENTS.md, USER_PRINCIPLES.md, PROJECT_INTENT.md, .gitignore" in second_output


def test_init_rejects_missing_directory(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing"

    assert main(["init", str(missing)]) == 2

    assert "项目目录不存在" in capsys.readouterr().err
    assert not missing.exists()


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
    assert "SessionStart" in content
    assert "workspace finalize REQ-ID" in content
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

    subprocess.run([str(ai_dev_os), "init", str(project)], check=True, capture_output=True)
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
    )

    hooks = json.loads((project / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert "REQ-001" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "ai-dev-os hook"
    assert not (project / "src").exists()
    assert not (project / ".venv").exists()
    assert shutil.which("python", path=str(project)) is None
