"""Phase 1 的产品入口、旧配置与非 Codex Dispatcher 集成。"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_dispatcher import FakeTasks
from test_runtime_codex import runtime as codex_fixture_runtime

from workspace_orchestrator import composition, product_cli
from workspace_orchestrator.agent_runtime.claude import ClaudeCliRuntime
from workspace_orchestrator.agent_runtime.contracts import (
    STANDARD_EVENT_KINDS,
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    RuntimeDescriptor,
)
from workspace_orchestrator.agent_runtime.cursor import CursorAcpRuntime
from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
from workspace_orchestrator.automation import dispatcher
from workspace_orchestrator.models import Task
from workspace_orchestrator.project_config import initialized_project_config, load_project_config
from workspace_orchestrator.project_init import initialize_project
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


def _fixture_runtime(name, event_sink):
    """只替换外部产品进程命令，保留真实 Adapter、传输和执行端口。"""
    if name == "codex":
        return codex_fixture_runtime(event_sink=event_sink)
    adapter = {"cursor": CursorAcpRuntime, "claude": ClaudeCliRuntime}[name]
    fixture = Path(__file__).parent / "fixtures" / f"runtime_{name}_server.py"
    return adapter(
        [sys.executable, "-X", "utf8", "-u", str(fixture), "normal"],
        event_sink=event_sink, timeout_seconds=3,
    )


@pytest.mark.parametrize("name", ["cursor", "claude"])
def test_managed_hooks_configured_dispatcher_runs_non_codex_fixture_without_completion(
    tmp_path, monkeypatch, name,
):
    initialized = initialize_project(tmp_path)
    assert ".codex/hooks.json" in initialized.created
    assert dispatcher._only_managed_hooks(tmp_path)
    hooks_before = (tmp_path / ".codex" / "hooks.json").read_bytes()
    _config(tmp_path, agent_runtime=name, agent_model="model-b", agent_sandbox="read-only")
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create(
        "托管 Hook 下的非 Codex 执行", task_provider="dashi", task_project_id="demo"
    )
    store.touch_meta(requirement_id, status="in_progress")
    tasks = FakeTasks(Task(
        id="TASK-MANAGED", title="只读协议工作", status="in_progress",
        labels=(f"requirement:{requirement_id}",), version=1,
    ))
    created = []

    def fixture_factory(selected, *, event_sink=None):
        assert selected == name
        adapter = _fixture_runtime(selected, event_sink)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(composition, "create_runtime", fixture_factory)
    monkeypatch.setattr(dispatcher, "configured_task_provider", lambda meta, root: tasks)
    executor = composition.configured_executor(store)
    assert not executor.allow_managed_hook_trust
    engine = dispatcher.AutoDispatcher(store, executor)

    # 真实零退出码不能替代 Review：任务未进入审查时，Dispatcher 必须阻塞而非完成。
    assert engine.run_once() == "blocked"
    assert engine.run_once() == "idle"
    assert len(created) == 1
    adapter = created[0]
    assert adapter._request.bypass_hook_trust is False
    assert adapter._request.sandbox == "read-only"
    assert adapter._request.model == "model-b"
    assert requirement_id in adapter._request.prompt and "TASK-MANAGED" in adapter._request.prompt
    assert adapter._client.pid is not None and not adapter._client.running
    log_path = next((store.root / "dispatcher-logs").glob("*.json"))
    logged = json.loads(log_path.read_text(encoding="utf-8"))
    assert logged["returncode"] == 0
    assert logged["runtime_id"] == name and logged["session_id"]
    assert logged["summary"] == "streamed 中文"
    events = RuntimeEventStore(store.root / "runtime-events").replay(logged["run_id"])
    assert {"message", "approval", "completion", "unknown"} <= {event.kind for event in events}
    assert all(event.kind in STANDARD_EVENT_KINDS for event in events)
    assert tasks.task.status == "blocked"
    assert any("Task 未进入 review" in message for message in tasks.added_comments)
    assert store.load(requirement_id)["meta"]["status"] == "in_progress"
    assert (tmp_path / ".codex" / "hooks.json").read_bytes() == hooks_before


def _consume_by_standard_kind(events):
    """公共消费者只识别标准事件类别，不解析产品名称或具体 wire method。"""
    buckets = {kind: [] for kind in ("message", "completion", "unknown")}
    for event in events:
        assert event.kind in STANDARD_EVENT_KINDS
        if event.kind in buckets:
            buckets[event.kind].append(event.payload)
    return buckets


def _nested_payload(payload, path):
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


@pytest.mark.parametrize("name,unknown_path,expected_unknown", [
    ("codex", ("params", "opaque"), {"nested": [1, "two"]}),
    ("cursor", ("params", "update", "future"), {"unchanged": [1, {"nested": True}]}),
    ("claude", ("nested",), {"retained": [1, 2]}),
])
def test_all_adapters_share_kind_only_consumer_and_lossless_unknown_replay(
    tmp_path, name, unknown_path, expected_unknown,
):
    original = []
    store = RuntimeEventStore(tmp_path / "events")

    def persist(event):
        store.append(event)
        original.append(event)

    adapter = _fixture_runtime(name, persist)
    run_id = "shared-consumer"
    try:
        opened = adapter.start(AgentRunRequest(
            run_id, tmp_path, "只读合同测试", sandbox="read-only", timeout_seconds=5,
        ))
        assert opened.ok and opened.session and opened.turn_id
        result = adapter.wait(opened.session, opened.turn_id, timeout_seconds=5)
        assert result.returncode == 0, result
        assert adapter._client.running  # 不等待 Agent 进程退出，就能消费持久事件。
        replayed = store.replay(run_id)
        buckets = _consume_by_standard_kind(replayed)
        assert all(buckets[kind] for kind in ("message", "completion", "unknown"))
        assert len(buckets["completion"]) == 1
        assert buckets == _consume_by_standard_kind(original)
        # 包括未知嵌套数据在内的整个 wire payload 都必须原样往返，而不仅是摘要文本。
        assert [event.payload for event in replayed] == [event.payload for event in original]
        assert any(
            _nested_payload(payload, unknown_path) == expected_unknown
            for payload in buckets["unknown"]
        )
        assert all(event.run_id == run_id for event in replayed)
        assert [event.sequence for event in replayed] == list(range(1, len(replayed) + 1))
    finally:
        adapter.close()
