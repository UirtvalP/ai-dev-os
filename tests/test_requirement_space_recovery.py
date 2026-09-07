from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from workspace_orchestrator.adapters.base import TaskProviderError
from workspace_orchestrator.automation.task_attach import (
    BoardTaskSyncError,
    ensure_requirement_board_task,
    ensure_requirement_space_task,
)
from workspace_orchestrator.models import Task
from workspace_orchestrator.phase_gate import GateStore
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


class LegacySpaceTasks:
    def __init__(self, task: Task) -> None:
        self.task = task
        self.created = 0
        self.repairs = 0

    def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
        label = f"requirement:{requirement_id}"
        return (self.task,) if label in self.task.labels else ()

    def get_task(self, task_id: str) -> Task:
        if self.task.id != task_id:
            raise TaskProviderError("not found")
        return self.task

    def find_tasks_by_exact_title(self, title: str) -> tuple[Task, ...]:
        return (self.task,) if self.task.title == title else ()

    def create_task(self, requirement_id: str, task: Task) -> Task:
        self.created += 1
        self.task = replace(task, id="NEW", project_id="demo", version=1)
        return self.task

    def reconcile_task(self, requirement_id: str, task_id: str, desired: Task) -> Task:
        assert task_id == self.task.id
        if (
            self.task.description == desired.description
            and self.task.status == desired.status
            and set(desired.labels).issubset(self.task.labels)
        ):
            return self.task
        self.repairs += 1
        self.task = replace(
            desired,
            id=task_id,
            project_id="demo",
            version=(self.task.version or 0) + 1,
        )
        return self.task


class DuplicateSpaceTasks:
    def __init__(self, tasks: tuple[Task, ...]) -> None:
        self.tasks = {task.id: task for task in tasks}
        self.created = 0

    def list_tasks(self, requirement_id: str) -> tuple[Task, ...]:
        label = f"requirement:{requirement_id}"
        return tuple(task for task in self.tasks.values() if label in task.labels)

    def get_task(self, task_id: str) -> Task:
        return self.tasks[task_id]

    def find_tasks_by_exact_title(self, title: str) -> tuple[Task, ...]:
        return tuple(task for task in self.tasks.values() if task.title == title)

    def create_task(self, requirement_id: str, task: Task) -> Task:
        self.created += 1
        raise AssertionError("duplicate recovery must not create")

    def reconcile_task(self, requirement_id: str, task_id: str, desired: Task) -> Task:
        raise AssertionError("duplicate recovery must fail before reconcile")


def _store(tmp_path: Path) -> tuple[WorkspaceStore, str, str]:
    store = WorkspaceStore(tmp_path)
    requirement_id = store.create(
        "V2 delivery",
        task_provider=None,
        task_project_id="demo",
    )
    title = f"[需求空间] {requirement_id} V2 delivery"
    return store, requirement_id, title


def test_meta_pointer_recovers_and_repairs_legacy_unlabelled_space_card(
    tmp_path: Path,
) -> None:
    store, requirement_id, title = _store(tmp_path)
    task = Task(
        id="AID-150",
        title=title,
        description=f"# {requirement_id} 需求空间",
        status="backlog",
        project_id="demo",
        version=1,
    )
    tasks = LegacySpaceTasks(task)
    store.touch_meta(requirement_id, requirement_space_task_id=task.id)

    first = ensure_requirement_space_task(store, requirement_id, tasks)  # type: ignore[arg-type]
    second = ensure_requirement_space_task(store, requirement_id, tasks)  # type: ignore[arg-type]

    assert first is not None and second is not None
    assert first.id == second.id == "AID-150"
    assert tasks.created == 0
    assert tasks.repairs == 1
    assert tasks.task.status == "todo"
    assert set(tasks.task.labels) == {
        "requirement-space",
        f"requirement:{requirement_id}",
    }
    assert store.load(requirement_id)["meta"]["requirement_space_task_id"] == "AID-150"


def test_missing_pointer_target_fails_closed_without_creating_duplicate(tmp_path: Path) -> None:
    store, requirement_id, title = _store(tmp_path)
    tasks = LegacySpaceTasks(
        Task(
            id="OTHER",
            title=title,
            description=f"# {requirement_id} 需求空间",
            project_id="demo",
        )
    )
    store.touch_meta(requirement_id, requirement_space_task_id="MISSING")

    with pytest.raises(BoardTaskSyncError, match="避免创建重复卡"):
        ensure_requirement_space_task(store, requirement_id, tasks)  # type: ignore[arg-type]

    assert tasks.created == 0


@pytest.mark.parametrize("duplicate_labels", [(), ("requirement-space", "requirement:REQ-001")])
def test_pointer_cannot_hide_second_labeled_or_legacy_space_card(
    tmp_path: Path, duplicate_labels: tuple[str, ...]
) -> None:
    store, requirement_id, title = _store(tmp_path)
    canonical_labels = ("requirement-space", f"requirement:{requirement_id}")
    if duplicate_labels:
        duplicate_labels = ("requirement-space", f"requirement:{requirement_id}")
    tasks = DuplicateSpaceTasks(
        (
            Task(
                id="AID-1",
                title=title,
                description=f"# {requirement_id} 需求空间",
                status="todo",
                project_id="demo",
                labels=canonical_labels,
                version=1,
            ),
            Task(
                id="AID-2",
                title=title,
                description=f"# {requirement_id} 需求空间",
                status="backlog",
                project_id="demo",
                labels=duplicate_labels,
                version=1,
            ),
        )
    )
    store.touch_meta(requirement_id, requirement_space_task_id="AID-1")

    with pytest.raises(WorkspaceError, match="AID-1, AID-2"):
        ensure_requirement_space_task(store, requirement_id, tasks)  # type: ignore[arg-type]

    assert tasks.created == 0


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "other"},
        {"title": "[需求空间] REQ-999 Wrong"},
        {"description": "# REQ-999 需求空间"},
        {"binding_session_id": "worker"},
        {"labels": ("requirement-space", "requirement:REQ-999")},
    ],
)
def test_pointer_identity_conflict_never_overwrites_or_creates(
    tmp_path: Path, change: dict[str, object]
) -> None:
    store, requirement_id, title = _store(tmp_path)
    task = replace(
        Task(
            id="AID-150",
            title=title,
            description=f"# {requirement_id} 需求空间",
            status="backlog",
            project_id="demo",
            version=1,
        ),
        **change,
    )
    tasks = LegacySpaceTasks(task)
    store.touch_meta(requirement_id, requirement_space_task_id=task.id)

    with pytest.raises(WorkspaceError):
        ensure_requirement_space_task(store, requirement_id, tasks)  # type: ignore[arg-type]

    assert tasks.created == 0
    assert tasks.repairs == 0


def test_gated_board_sync_never_creates_or_retargets_missing_authority(
    tmp_path: Path,
) -> None:
    store, requirement_id, _ = _store(tmp_path)
    tasks = DuplicateSpaceTasks(
        (
            Task(
                id="AID-UNDECLARED",
                title="unrelated work",
                status="todo",
                labels=(f"requirement:{requirement_id}",),
            ),
        )
    )
    store.touch_meta(
        requirement_id,
        phase_gate_required=True,
        requirement_task_id="AID-DECLARED",
        pending_task_visibility=True,
    )

    with pytest.raises(BoardTaskSyncError, match="拒绝创建或改绑"):
        ensure_requirement_board_task(store, requirement_id, tasks)  # type: ignore[arg-type]

    meta = store.load(requirement_id)["meta"]
    assert meta["requirement_task_id"] == "AID-DECLARED"
    assert meta["pending_task_visibility"] is True
    assert tasks.created == 0


def test_gated_board_sync_only_projects_the_active_declared_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, requirement_id, _ = _store(tmp_path)
    declared = Task(
        id="AID-DECLARED",
        title="declared phase work",
        status="done",
        labels=(f"requirement:{requirement_id}",),
    )
    tasks = DuplicateSpaceTasks((declared,))
    store.touch_meta(
        requirement_id,
        phase_gate_required=True,
        requirement_task_id=declared.id,
        pending_task_visibility=True,
    )
    checked: list[tuple[str, str]] = []

    def require_active(_self: GateStore, req: str, task_id: str) -> str:
        checked.append((req, task_id))
        return "initial"

    monkeypatch.setattr(GateStore, "require_task_active", require_active)

    result = ensure_requirement_board_task(store, requirement_id, tasks)  # type: ignore[arg-type]

    assert result == declared
    assert checked == [(requirement_id, declared.id)]
    meta = store.load(requirement_id)["meta"]
    assert meta["requirement_task_id"] == declared.id
    assert meta["pending_task_visibility"] is False
    assert tasks.created == 0
