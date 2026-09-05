"""Phase 1 的产品入口、旧配置与非 Codex Dispatcher 集成。"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

from workspace_orchestrator import product_cli
from workspace_orchestrator.agent_runtime.contracts import (
    AgentEvent,
    AgentRunResult,
    RuntimeDescriptor,
)
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
from workspace_orchestrator.automation import dispatcher
from workspace_orchestrator.models import Task
from workspace_orchestrator.project_config import initialized_project_config, load_project_config
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def _config(tmp_path, **changes):
    data = initialized_project_config(tmp_path)
    data.update(changes)
    (tmp_path / ".ai-dev-os.json").write_text(json.dumps(data), encoding="utf-8")


def test_legacy_project_config_keeps_codex_settings(tmp_path):
    _config(tmp_path, codex_model="old-model", codex_sandbox="read-only", future={"keep": True})
    before = (tmp_path / ".ai-dev-os.json").read_bytes()
    config = load_project_config(tmp_path)
    assert config.agent_runtime == "codex"
    assert config.agent_model is None and config.agent_sandbox is None
    assert config.codex_model == "old-model" and config.codex_sandbox == "read-only"
    assert (tmp_path / ".ai-dev-os.json").read_bytes() == before


@pytest.mark.parametrize("name", ["codex", "cursor", "claude"])
def test_explicit_runtime_configuration(tmp_path, name):
    _config(tmp_path, agent_runtime=name, agent_model="chosen", agent_sandbox="read-only")
    config = load_project_config(tmp_path)
    assert config.agent_runtime == name and config.agent_model == "chosen"
    assert config.agent_sandbox == "read-only"


@pytest.mark.parametrize("field,value", [
    ("agent_runtime", []), ("agent_runtime", "unknown"), ("agent_runtime", None),
    ("agent_model", ""), ("agent_model", 10),
    ("agent_sandbox", {}), ("agent_sandbox", "unconfined"),
])
def test_invalid_runtime_configuration_fails_closed(tmp_path, field, value):
    _config(tmp_path, **{field: value})
    with pytest.raises(WorkspaceError, match=field):
        load_project_config(tmp_path)


def test_runtime_cli_reports_unavailable_honestly(monkeypatch, capsys):
    monkeypatch.setattr(product_cli, "runtime_descriptors", lambda: (
        RuntimeDescriptor("cursor", "Cursor", "unknown", False, reason="未安装"),
    ))
    assert product_cli.main(["runtime", "list"]) == 0
    item = json.loads(capsys.readouterr().out)[0]
    assert item["available"] is False and item["capabilities"] == []


def test_runtime_cli_replays_ordered_events_with_cursor(tmp_path, capsys):
    store = WorkspaceStore(tmp_path)
    store.create("保留旧Workspace")
    events = RuntimeEventStore(store.root / "runtime-events")
    events.append(AgentEvent("one", "run", "fake", "message.delta", {"text": "一"}))
    events.append(AgentEvent("two", "run", "fake", "message.completed", {"text": "二"}))
    assert product_cli.main(["runtime", "events", "run", "--root", str(tmp_path), "--after", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result) == 1 and result[0]["event_id"] == "two"
    assert product_cli.main(["runtime", "events", "../bad", "--root", str(tmp_path)]) == 2
    assert "run_id" in capsys.readouterr().err


def test_dispatcher_has_no_concrete_runtime_import_or_wire_parser():
    tree = ast.parse(Path(dispatcher.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").endswith(("adapters.agent", "agent_runtime.codex", "agent_runtime.cursor", "agent_runtime.claude"))
    assert "item.completed" not in Path(dispatcher.__file__).read_text(encoding="utf-8")


def test_non_codex_executor_drives_dispatcher_and_cannot_complete_requirement(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path)
    req = store.create("非Codex交付", task_provider="dashi", task_project_id="demo")
    store.touch_meta(req, status="in_progress")
    _config(tmp_path, agent_runtime="cursor", agent_model="selected", agent_sandbox="read-only")

    class Tasks:
        task = Task("TASK-1", "非Codex", status="in_progress", labels=(f"requirement:{req}",), version=1)
        def __init__(self):
            self.comments = []

        def list_tasks(self, requirement_id):
            return (self.task,)

        def list_comments(self, task_id):
            return ()

        def get_task(self, task_id):
            return self.task

        def update_status(self, task_id, status):
            self.task = replace(self.task, status=status, version=self.task.version + 1)
            return self.task

        def add_comment(self, task_id, body):
            self.comments.append(body)

        def unlink_session(self, task_id, session_id):
            pass

    tasks = Tasks()

    class NonCodexExecutor:
        def execute(self, workspace_path, prompt, **options):
            assert options["model"] == "selected"
            assert options["sandbox"] == "read-only"
            assert req in prompt
            tasks.update_status(tasks.task.id, "in_review")
            return AgentRunResult(0, "cursor-session", "非JSON消息", "", runtime_id="cursor", run_id="r1", summary="实现候选")

    monkeypatch.setattr(dispatcher, "configured_task_provider", lambda meta, root: tasks)
    engine = dispatcher.AutoDispatcher(store, NonCodexExecutor())
    assert engine.run_once() == "completed"
    assert engine.run_once() == "idle"
    assert tasks.task.status == "in_review"
    assert store.load(req)["meta"]["status"] == "in_progress"
    log_path = next((store.root / "dispatcher-logs").glob("*.json"))
    logged = json.loads(log_path.read_text(encoding="utf-8"))
    assert logged["runtime_id"] == "cursor" and logged["run_id"] == "r1"
    assert logged["summary"] == "实现候选"
