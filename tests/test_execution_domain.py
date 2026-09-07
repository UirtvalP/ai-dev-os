from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from workspace_orchestrator.agent_runtime.events import RuntimeEventStore
from workspace_orchestrator.cli import main
from workspace_orchestrator.executions import ExecutionService, ExecutionStore
from workspace_orchestrator.models import WorkflowComplexity
from workspace_orchestrator.workspace import WorkspaceStore


def test_fake_runtime_demo_is_traceable_end_to_end(tmp_path):
    workspace = WorkspaceStore(tmp_path)
    requirement_id = workspace.create(
        "Execution demo", complexity=WorkflowComplexity.COMPLEX, task_provider=None,
    )
    executions = ExecutionStore(workspace)
    events = RuntimeEventStore(workspace.root / "runtime-events")

    completed = ExecutionService(executions, events).demo(
        requirement_id, task_id="TASK-001"
    )

    assert completed.status == "completed"
    assert completed.session_id and completed.id != completed.session_id
    assert executions.get(completed.id) == completed
    assert executions.list(requirement_id) == (completed,)
    replayed = events.replay(completed.id)
    assert [event.sequence for event in replayed] == [1, 2]
    assert all(event.requirement_id == requirement_id for event in replayed)
    assert all(event.task_id == "TASK-001" for event in replayed)
    assert all(event.execution_id == completed.id for event in replayed)
    assert all(event.runtime_id == "fake" for event in replayed)
    assert all(event.session_id == completed.session_id for event in replayed)
    assert events.query(requirement_id=requirement_id) == replayed
    assert events.query(task_id="TASK-001") == replayed
    assert events.query(execution_id=completed.id) == replayed
    assert events.query(runtime_id="fake") == replayed
    assert events.query(session_id=completed.session_id) == replayed
    assert events.query(requirement_id=requirement_id, after=1, limit=1) == replayed[1:]


def test_execution_ids_are_unique_across_requirements(tmp_path):
    workspace = WorkspaceStore(tmp_path)
    first = workspace.create("First", task_provider=None)
    second = workspace.create("Second", task_provider=None)
    executions = ExecutionStore(workspace)

    one = executions.create(
        first, "TASK-1", role="implementation", runtime_id="fake", prompt="one"
    )
    two = executions.create(
        second, "TASK-2", role="implementation", runtime_id="fake", prompt="two"
    )

    assert one.id == "EXE-000001"
    assert two.id == "EXE-000002"
    assert executions.get(one.id).requirement_id == first
    assert executions.get(two.id).requirement_id == second

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(
            lambda index: executions.create(
                first if index % 2 else second, f"TASK-{index + 10}",
                role="implementation", runtime_id="fake", prompt=str(index),
            ),
            range(20),
        ))
    assert len({item.id for item in created}) == 20
    assert len(executions.list(first)) + len(executions.list(second)) == 22


def test_legacy_session_mapping_is_idempotent_and_non_destructive(tmp_path):
    workspace = WorkspaceStore(tmp_path)
    requirement_id = workspace.create("Legacy", task_provider=None)
    sessions_path = workspace.path_for(requirement_id) / "sessions.json"
    legacy = [{
        "id": "codex-old", "agent": "claude",
        "task_ids": ["TASK-OLD", "TASK-SECOND"], "result": "in_progress",
        "started_at": "2026-01-01T00:00:00+00:00", "unknown": {"keep": True},
    }]
    workspace.write_json(sessions_path, legacy)
    executions = ExecutionStore(workspace)
    service = ExecutionService(
        executions, RuntimeEventStore(workspace.root / "runtime-events")
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.map_legacy_sessions(requirement_id), range(8)))
    first = results[0]
    second = service.map_legacy_sessions(requirement_id)

    assert first == second
    assert len(first) == 2
    assert {item.task_id for item in first} == {"TASK-OLD", "TASK-SECOND"}
    assert all(item.source == "legacy-thread-binding" for item in first)
    assert all(item.session_id == "codex-old" for item in first)
    assert all(item.runtime_id == "claude" for item in first)
    assert all(item.status == "running" for item in first)
    assert len(executions.list(requirement_id)) == 2
    assert workspace.read_json(sessions_path) == legacy


def test_execution_cli_demo_list_and_show(tmp_path, capsys):
    workspace = WorkspaceStore(tmp_path)
    requirement_id = workspace.create("CLI", task_provider=None)

    assert main(["--root", str(tmp_path), "execution", "demo", requirement_id]) == 0
    created = capsys.readouterr().out
    assert '"status": "completed"' in created
    execution_id = ExecutionStore(workspace).list(requirement_id)[0].id

    assert main(["--root", str(tmp_path), "execution", "list", requirement_id]) == 0
    assert execution_id in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "execution", "show", execution_id]) == 0
    assert '"runtime_id": "fake"' in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "execution", "events", execution_id]) == 0
    events = capsys.readouterr().out
    assert '"execution_id":' in events and '"session_id":' in events
