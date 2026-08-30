from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from workspace_orchestrator import project_registry, user_config
from workspace_orchestrator.product_cli import main
from workspace_orchestrator.project_config import (
    ProjectConfig,
    default_project_config,
    project_display_name,
)
from workspace_orchestrator.project_init import initialize_project
from workspace_orchestrator.project_registry import GlobalProjectRegistry
from workspace_orchestrator.workspace import WorkspaceError


def _payload() -> dict[str, object]:
    return json.loads(user_config.projects_registry_path().read_text(encoding="utf-8"))


def _project(root: Path, name: str = "project") -> Path:
    path = root / name
    path.mkdir(parents=True)
    return path


def test_init_registers_multiple_projects_without_overwriting(tmp_path: Path) -> None:
    first = _project(tmp_path / "one", "app")
    second = _project(tmp_path / "two", "app")

    assert main(["init", str(first)]) == 0
    assert main(["init", str(second)]) == 0

    payload = _payload()
    projects = payload["projects"]
    assert payload["schema_version"] == 1
    assert len(projects) == 2
    assert {item["path"] for item in projects} == {str(first.resolve()), str(second.resolve())}
    assert len({item["id"] for item in projects}) == 2


def test_repeated_init_is_idempotent_and_preserves_registered_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _project(tmp_path)
    timestamps = iter(("2026-08-30T01:00:00+08:00", "2026-08-30T02:00:00+08:00"))
    monkeypatch.setattr(project_registry, "now_iso", lambda: next(timestamps))

    assert main(["init", str(path)]) == 0
    first = _payload()["projects"][0]
    assert main(["init", str(path)]) == 0
    second = _payload()["projects"][0]

    assert len(_payload()["projects"]) == 1
    assert second == first
    assert second["updated_at"] == "2026-08-30T01:00:00+08:00"


def test_project_move_updates_path_under_persisted_identity(tmp_path: Path) -> None:
    original = _project(tmp_path / "old", "app")
    assert main(["init", str(original)]) == 0
    project_id = _payload()["projects"][0]["id"]

    moved = tmp_path / "new" / "app"
    moved.parent.mkdir(parents=True)
    original.rename(moved)

    assert main(["init", str(moved)]) == 0

    projects = _payload()["projects"]
    assert len(projects) == 1
    assert projects[0]["id"] == project_id
    assert projects[0]["path"] == str(moved.resolve())


def test_project_list_marks_missing_path_without_deleting_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _project(tmp_path)
    assert main(["init", str(path)]) == 0
    capsys.readouterr()
    project_id = _payload()["projects"][0]["id"]
    shutil.rmtree(path)

    assert main(["project", "list"]) == 0

    output = capsys.readouterr().out
    assert project_id in output
    assert "missing" in output
    assert len(_payload()["projects"]) == 1


def test_project_show_and_unregister_preserve_local_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _project(tmp_path)
    marker = path / "business.txt"
    marker.write_text("keep\n", encoding="utf-8")
    assert main(["init", str(path)]) == 0
    capsys.readouterr()
    project_id = _payload()["projects"][0]["id"]

    assert main(["project", "show", project_id]) == 0
    show_output = capsys.readouterr().out
    assert f"ID: {project_id}" in show_output
    assert f"Path: {path.resolve()}" in show_output

    assert main(["project", "unregister", project_id]) == 0
    unregister_output = capsys.readouterr().out

    assert "项目文件、.workspace、Git 与 dashi Task 均未删除" in unregister_output
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert (path / ".ai-dev-os.json").is_file()
    assert _payload()["projects"] == []


def test_migrate_refreshes_task_metadata_without_changing_project_identity(
    tmp_path: Path,
) -> None:
    path = _project(tmp_path)
    assert main(["init", str(path)]) == 0
    original = _payload()["projects"][0]
    config_path = path / ".ai-dev-os.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["task_project_id"] = "renamed-dashi-project"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert main(["migrate", str(path)]) == 0

    projects = _payload()["projects"]
    assert len(projects) == 1
    assert projects[0]["id"] == original["id"]
    assert projects[0]["task_project_id"] == "renamed-dashi-project"


def test_migrate_registers_legacy_initialized_project(tmp_path: Path) -> None:
    path = _project(tmp_path)
    initialize_project(path)
    assert not user_config.projects_registry_path().exists()

    assert main(["migrate", str(path)]) == 0

    assert _payload()["projects"][0]["path"] == str(path.resolve())


def test_registry_failure_does_not_roll_back_successful_project_init(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _project(tmp_path)
    registry_path = user_config.projects_registry_path()
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("not-json\n", encoding="utf-8")

    assert main(["init", str(path)]) == 0

    output = capsys.readouterr().out
    assert "项目接入成功，但全局注册失败" in output
    assert "再次运行同一命令可重试" in output
    assert (path / ".ai-dev-os.json").is_file()
    assert (path / "PROJECT_INTENT.md").is_file()


def test_registry_rejects_same_path_with_unrelated_identity(tmp_path: Path) -> None:
    path = _project(tmp_path)
    registry = GlobalProjectRegistry()
    registry.register(path, ProjectConfig("dashi", "first", project_id="first"))

    with pytest.raises(WorkspaceError, match="不能猜测"):
        registry.register(path, ProjectConfig("dashi", "second", project_id="second"))


def test_registry_rejects_relative_persisted_path(tmp_path: Path) -> None:
    registry = GlobalProjectRegistry()
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [
                    {
                        "id": "demo",
                        "name": "Demo",
                        "path": "relative/demo",
                        "task_provider": "dashi",
                        "task_project_id": "demo",
                        "registered_at": "2026-08-30T01:00:00+08:00",
                        "updated_at": "2026-08-30T01:00:00+08:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="绝对路径"):
        registry.list()


def test_concurrent_project_registration_does_not_lose_updates(tmp_path: Path) -> None:
    paths = [_project(tmp_path / str(index), "app") for index in range(8)]
    registry = GlobalProjectRegistry()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda path: registry.register(path, default_project_config(path)), paths))

    projects = registry.list()
    assert len(projects) == len(paths)
    assert {item.path for item in projects} == {str(path.resolve()) for path in paths}


def test_project_display_name_prefers_pyproject_then_package_json(tmp_path: Path) -> None:
    path = _project(tmp_path)
    (path / "package.json").write_text('{"name": "package-name"}\n', encoding="utf-8")
    assert project_display_name(path) == "package-name"

    (path / "pyproject.toml").write_text(
        '[project]\nname = "python-name"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    assert project_display_name(path) == "python-name"
