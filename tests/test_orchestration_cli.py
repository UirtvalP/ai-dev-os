"""编排产品入口与前台服务的离线测试，不启动真实 Runtime 或任务面板。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from workspace_orchestrator import orchestration_composition as composition
from workspace_orchestrator import product_cli
from workspace_orchestrator.agent_runtime.contracts import ModelDescriptor, RuntimeDescriptor
from workspace_orchestrator.orchestration.contracts import (
    ModelRoute,
    PlanningRequest,
    TaskSpec,
    WorkerIsolation,
    WorkerObservation,
)
from workspace_orchestrator.orchestration.supervisor import RequirementSupervisor
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


def _forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("编排入口测试不得调用真实模型、隔离进程或任务面板")


@pytest.fixture(autouse=True)
def prohibit_live_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(composition, "runtime_descriptors", _forbidden)
    monkeypatch.setattr(composition, "create_runtime", _forbidden)
    monkeypatch.setattr(product_cli, "runtime_descriptors", _forbidden)


def _descriptors() -> tuple[RuntimeDescriptor, ...]:
    return (RuntimeDescriptor(
        "fixture", "Fixture Runtime", "1", True,
        ("start", "message", "events", "profile:read-only"),
        (ModelDescriptor("fixture-model", "Fixture Model"),),
    ),)


@dataclass
class CliWorkers:
    """只在内存记录执行；terminal 观测表示此假件已确认执行结束。"""

    enforced: bool = True
    finish_on_poll: bool = True
    observations: dict[str, WorkerObservation] = field(default_factory=dict)
    dispatches: list[tuple[str, int, str]] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def isolation(self, task: TaskSpec) -> WorkerIsolation:
        self.probes.append(task.task_id)
        return WorkerIsolation("fixture-only", self.enforced, (), (), reason="测试隔离能力")

    def dispatch(self, attempt_id: str, fence: int, task: TaskSpec, route: ModelRoute) -> WorkerObservation:
        assert attempt_id not in self.observations
        self.dispatches.append((attempt_id, fence, task.task_id))
        observation = WorkerObservation(attempt_id, fence, "running")
        self.observations[attempt_id] = observation
        return observation

    def poll(self, attempt_id: str, fence: int) -> WorkerObservation:
        if self.finish_on_poll:
            self.observations[attempt_id] = WorkerObservation(
                attempt_id, fence, "candidate_complete", candidate_sha="a" * 40,
                candidate_tree="b" * 40, summary="假执行端候选，尚未验收",
            )
        return self.observations[attempt_id]

    def reconcile(self, attempt_id: str, fence: int) -> WorkerObservation:
        return self.observations.get(attempt_id, WorkerObservation(attempt_id, fence, "unknown"))

    def cancel(self, attempt_id: str, fence: int) -> WorkerObservation:
        self.cancelled.append(attempt_id)
        result = WorkerObservation(attempt_id, fence, "failed", error_class="cancelled")
        self.observations[attempt_id] = result
        return result


@dataclass
class CliWorkspace:
    root: Path
    workspace: WorkspaceStore
    requirement_id: str
    planning: PlanningRequest
    file: Path
    workers: CliWorkers
    configured: list[dict[str, Any]] = field(default_factory=list)
    supervisors: list[RequirementSupervisor] = field(default_factory=list)

    def args(self, command: str, *extra: str) -> list[str]:
        return ["orchestration", command, self.requirement_id, "--root", str(self.root), *extra]

    def facts(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.workspace.path_for(self.requirement_id).iterdir()
                if path.is_file()}


@pytest.fixture
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliWorkspace:
    project, task_root = tmp_path / "project", tmp_path / "task-worktrees" / "task-1"
    project.mkdir()
    task_root.mkdir(parents=True)
    workspace = WorkspaceStore(project)
    requirement_id = workspace.create("保留已有 Requirement", task_provider=None)
    workspace.touch_meta(requirement_id, status="in_progress")
    planning = PlanningRequest(requirement_id, "控制面入口测试", (
        TaskSpec("task-1", "只读任务", "生成实现候选，不授予完成权限", worktree=str(task_root)),
    ))
    file = tmp_path / "plan.json"
    file.write_text(json.dumps(planning.to_dict(), ensure_ascii=False), encoding="utf-8")
    result = CliWorkspace(project, workspace, requirement_id, planning, file, CliWorkers())

    def configured(store: WorkspaceStore, requirement_id: str, **options: Any) -> RequirementSupervisor:
        result.configured.append(options)
        control = composition.control_store(store, requirement_id)
        supervisor = RequirementSupervisor(
            control, owner=options["owner"], workers=result.workers, runtimes=_descriptors,
            max_workers=options["max_workers"], protected_roots=(store.root, store.project_root),
            allowed_worktree_roots=options["allowed_worktree_roots"],
        )
        result.supervisors.append(supervisor)
        return supervisor

    monkeypatch.setattr(composition, "configured_supervisor", configured)
    return result


def test_plan_cli_only_persists_plan_and_releases_lease(
    cli_workspace: CliWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = cli_workspace
    before = setup.facts()
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["plan"]["requirement_id"] == setup.requirement_id
    assert output["data"]["nodes"]["task-1"]["status"] == "pending"
    persisted = composition.control_store(setup.workspace, setup.requirement_id).snapshot()
    assert persisted["lease"] is None
    assert setup.workers.dispatches == [] and setup.workers.probes == []
    assert setup.facts() == before
    assert setup.workspace.load(setup.requirement_id)["meta"]["status"] == "in_progress"


def test_plan_cli_forwards_explicit_operator_limits_without_starting_workers(
    cli_workspace: CliWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = cli_workspace
    allowed = Path(setup.planning.tasks[0].worktree or "").parent
    assert product_cli.main(setup.args(
        "plan", "--owner", "explicit-controller", "--file", str(setup.file),
        "--max-workers", "3", "--allow-worktree-root", str(allowed), "--allow-network",
    )) == 0
    capsys.readouterr()
    assert setup.configured == [{
        "owner": "explicit-controller", "max_workers": 3, "allow_network": True,
        "allowed_worktree_roots": (allowed,),
    }]
    assert setup.workers.dispatches == []


def test_status_cli_is_pure_read_and_does_not_construct_a_supervisor(
    cli_workspace: CliWorkspace, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = cli_workspace
    before = setup.facts()
    monkeypatch.setattr(composition, "configured_supervisor", _forbidden)
    assert product_cli.main(setup.args("status")) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["revision"] == 0 and output["data"] == {} and output["lease"] is None
    assert not (setup.workspace.path_for(setup.requirement_id) / "orchestration").exists()
    assert setup.facts() == before


def test_status_cli_does_not_relaunch_a_persisted_plan(
    cli_workspace: CliWorkspace, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = cli_workspace
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 0
    capsys.readouterr()
    control = composition.control_store(setup.workspace, setup.requirement_id)
    before = control.snapshot()
    state_bytes = (control.root / "state.json").read_bytes()
    monkeypatch.setattr(composition, "configured_supervisor", _forbidden)
    assert product_cli.main(setup.args("status")) == 0
    assert json.loads(capsys.readouterr().out) == before
    assert (control.root / "state.json").read_bytes() == state_bytes
    assert setup.workers.dispatches == []


def test_plan_cli_rejects_request_for_another_requirement_without_acquiring(
    cli_workspace: CliWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = cli_workspace
    payload = setup.planning.to_dict()
    payload["requirement_id"] = "REQ-999"
    setup.file.write_text(json.dumps(payload), encoding="utf-8")
    before = setup.facts()
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 2
    assert "Requirement ID" in capsys.readouterr().err
    assert setup.supervisors[0].lease is None
    assert not composition.control_store(setup.workspace, setup.requirement_id).root.exists()
    assert setup.facts() == before
    assert setup.workers.dispatches == []


@pytest.mark.parametrize("payload", [b"{", b"[]", b'{"schema_version":2}', b"\xff"])
def test_plan_cli_reports_invalid_json_without_changing_requirement(
    cli_workspace: CliWorkspace, capsys: pytest.CaptureFixture[str], payload: bytes
) -> None:
    setup = cli_workspace
    setup.file.write_bytes(payload)
    before = setup.facts()
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 2
    assert capsys.readouterr().err.startswith("错误：")
    assert setup.facts() == before
    assert setup.workers.dispatches == []


def test_plan_cli_rejects_oversized_json_before_parsing(
    cli_workspace: CliWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = cli_workspace
    setup.file.write_bytes(b" " * (1024 * 1024 + 1))
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 2
    assert "1 MiB" in capsys.readouterr().err
    assert setup.workers.dispatches == []


@pytest.mark.parametrize("command", ["status", "plan", "run"])
def test_cli_requires_an_existing_requirement_and_never_creates_one(
    cli_workspace: CliWorkspace, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    setup = cli_workspace
    before = sorted(path.name for path in setup.workspace.root.iterdir())
    args = ["orchestration", command, "REQ-999", "--root", str(setup.root)]
    if command != "status":
        args.extend(["--owner", "controller"])
    if command == "plan":
        args.extend(["--file", str(setup.file)])
    assert product_cli.main(args) == 2
    assert "REQ-999" in capsys.readouterr().err
    assert sorted(path.name for path in setup.workspace.root.iterdir()) == before
    assert setup.workers.dispatches == []


class LoopClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def loop_clock(monkeypatch: pytest.MonkeyPatch) -> LoopClock:
    clock = LoopClock()
    # 只替换 composition 的时间依赖，不改 Python 全局 time 或文件锁时钟。
    monkeypatch.setattr(composition, "time", clock)
    return clock


def test_run_cli_reaches_candidate_and_keeps_requirement_in_progress(
    cli_workspace: CliWorkspace, loop_clock: LoopClock, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = cli_workspace
    before = setup.facts()
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 0
    capsys.readouterr()
    assert product_cli.main(setup.args("run", "--owner", "runner", "--timeout", "2")) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["data"]["nodes"]["task-1"]["status"] == "candidate_complete"
    assert len(setup.workers.dispatches) == 1 and loop_clock.sleeps
    assert composition.control_store(setup.workspace, setup.requirement_id).snapshot()["lease"] is None
    assert setup.facts() == before
    assert setup.workspace.load(setup.requirement_id)["meta"]["status"] == "in_progress"


def test_run_cli_honestly_reports_unavailable_isolation_without_launch(
    cli_workspace: CliWorkspace, loop_clock: LoopClock, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = cli_workspace
    setup.workers.enforced = False
    before = setup.facts()
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 0
    capsys.readouterr()
    assert product_cli.main(setup.args("run", "--owner", "runner", "--timeout", "2")) == 0
    output = capsys.readouterr().out
    state = json.loads(output)
    assert state["data"]["nodes"]["task-1"]["status"] == "blocked"
    assert "隔离" in state["data"]["nodes"]["task-1"]["reason"]
    assert setup.workers.dispatches == []
    assert loop_clock.sleeps == []
    assert setup.facts() == before


def test_run_cli_timeout_cancels_real_supervisor_fake_worker_before_release(
    cli_workspace: CliWorkspace, loop_clock: LoopClock, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = cli_workspace
    setup.workers.finish_on_poll = False
    before = setup.facts()
    assert product_cli.main(setup.args("plan", "--owner", "planner", "--file", str(setup.file))) == 0
    capsys.readouterr()
    assert product_cli.main(setup.args("run", "--owner", "runner", "--timeout", "0.3")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["nodes"]["task-1"]["active_attempt_id"] is None
    assert result["data"]["nodes"]["task-1"]["status"] == "stopped"
    assert setup.workers.cancelled == [setup.workers.dispatches[0][0]]
    assert composition.control_store(setup.workspace, setup.requirement_id).snapshot()["lease"] is None
    assert loop_clock.now >= 0.3
    assert setup.facts() == before


class RecordingSupervisor:
    """记录前台服务调用顺序，不代替领域 Supervisor 的状态机测试。"""

    def __init__(self, *, finish_after: int | None = 2) -> None:
        self.events: list[str] = []
        self.ticks = 0
        self.finish_after = finish_after
        self.held = False
        self.cancel_confirmed = True
        self.fail_initialize = False
        self.fail_renew = False
        self.state: dict[str, Any] = {"data": {"nodes": {
            "T": {"status": "running", "active_attempt_id": "fixture-attempt"},
        }}}

    def acquire(self) -> None:
        assert not self.held
        self.events.append("acquire")
        self.held = True

    def initialize(self, request: PlanningRequest) -> None:
        assert self.held
        self.events.append("initialize")
        if self.fail_initialize:
            raise WorkspaceError("初始化失败")

    def renew(self) -> None:
        assert self.held
        self.events.append("renew")
        if self.fail_renew:
            raise WorkspaceError("续租失败")

    def tick(self) -> dict[str, Any]:
        assert self.held
        assert self.events[-1] == "renew"
        self.events.append("tick")
        self.ticks += 1
        if self.finish_after is not None and self.ticks >= self.finish_after:
            self.state["data"]["nodes"]["T"].update(status="candidate_complete", active_attempt_id=None)
        return copy.deepcopy(self.state)

    def close(self, *, cancel_running: bool = False) -> None:
        self.events.append("close:cancel" if cancel_running else "close")
        if not self.held:
            return
        if self.state["data"]["nodes"]["T"]["active_attempt_id"] is not None:
            assert cancel_running
            if not self.cancel_confirmed:
                raise WorkspaceError("取消结果未知，仍保留槽位")
            self.state["data"]["nodes"]["T"].update(status="stopped", active_attempt_id=None)
        self.held = False

    def status(self) -> dict[str, Any]:
        self.events.append("status")
        return copy.deepcopy(self.state)


def test_foreground_runner_acquires_initializes_and_renews_before_every_tick(
    cli_workspace: CliWorkspace, loop_clock: LoopClock
) -> None:
    supervisor = RecordingSupervisor()
    result = composition.run_supervisor(supervisor, request=cli_workspace.planning,
                                        timeout_seconds=2, interval_seconds=0.1)
    assert supervisor.events == ["acquire", "initialize", "renew", "tick", "renew", "tick", "close:cancel"]
    assert not supervisor.held
    assert result["data"]["nodes"]["T"]["status"] == "candidate_complete"
    assert loop_clock.sleeps == [0.1]


def test_foreground_timeout_confirms_cancellation_and_returns_stopped_snapshot(loop_clock: LoopClock) -> None:
    supervisor = RecordingSupervisor(finish_after=None)
    state = composition.run_supervisor(supervisor, timeout_seconds=0.3, interval_seconds=0.2)
    assert supervisor.ticks == 3 and supervisor.events.count("renew") == 3
    assert not supervisor.held
    assert state["data"]["nodes"]["T"]["active_attempt_id"] is None
    assert state["data"]["nodes"]["T"]["status"] == "stopped"


def test_foreground_unknown_cancellation_cannot_report_success(loop_clock: LoopClock) -> None:
    supervisor = RecordingSupervisor(finish_after=None)
    supervisor.cancel_confirmed = False
    with pytest.raises(WorkspaceError, match="取消结果未知"):
        composition.run_supervisor(supervisor, timeout_seconds=0.1, interval_seconds=0.1)
    assert supervisor.held
    assert supervisor.state["data"]["nodes"]["T"]["active_attempt_id"] is not None


@pytest.mark.parametrize("failure", ["initialize", "renew"])
def test_foreground_failure_still_closes_owned_controller(
    cli_workspace: CliWorkspace, loop_clock: LoopClock, failure: str
) -> None:
    supervisor = RecordingSupervisor()
    setattr(supervisor, f"fail_{failure}", True)
    with pytest.raises(WorkspaceError, match="失败"):
        composition.run_supervisor(supervisor, request=cli_workspace.planning)
    assert supervisor.events[-1] == "close:cancel"
    assert not supervisor.held


@pytest.mark.parametrize("options", [
    {"timeout_seconds": 0}, {"timeout_seconds": -1}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": float("inf")}, {"timeout_seconds": 86401},
    {"interval_seconds": 0}, {"interval_seconds": 6}, {"interval_seconds": float("nan")},
])
def test_foreground_invalid_limits_do_not_acquire_authority(
    loop_clock: LoopClock, options: dict[str, Any]
) -> None:
    supervisor = RecordingSupervisor()
    with pytest.raises(WorkspaceError):
        composition.run_supervisor(supervisor, **options)
    assert supervisor.events == [] and not supervisor.held


def test_configured_composition_wires_trusted_ports_without_model_or_worker_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = WorkspaceStore(project)
    requirement_id = workspace.create("实际装配，假外部端口", task_provider=None)
    allowed = tmp_path / "worktrees"
    allowed.mkdir()
    captured: dict[str, Any] = {}
    workers = CliWorkers()

    def launcher(**options: Any) -> object:
        captured["launcher"] = options
        return SimpleNamespace(probe=_forbidden, launch=_forbidden)

    def worker_port(root: Path, **options: Any) -> CliWorkers:
        captured["workers"] = {"root": root, **options}
        return workers

    monkeypatch.setattr(composition, "WindowsAppContainerIsolation", launcher)
    monkeypatch.setattr(composition, "RuntimeWorkerPort", worker_port)
    monkeypatch.setattr(composition, "codex_command", lambda: (str(tmp_path / "tools" / "codex.exe"),))
    monkeypatch.setattr(composition.shutil, "which", lambda name: None)
    supervisor = composition.configured_supervisor(
        workspace, requirement_id, owner="operator", max_workers=2,
        allow_network=True, allowed_worktree_roots=(allowed,),
    )
    assert supervisor.lease is None and supervisor.workers is workers
    assert supervisor.runtimes is _forbidden
    assert supervisor.max_workers == 2 and supervisor.allowed_worktree_roots == (allowed,)
    assert captured["workers"]["requirement_id"] == requirement_id
    assert captured["workers"]["runtime_factory"] is _forbidden
    assert captured["workers"]["allow_network"] is True
    assert set(captured["workers"]["protected_roots"]) == {workspace.root, workspace.project_root}
    assert workers.dispatches == [] and workers.probes == []
    assert not composition.control_store(workspace, requirement_id).root.exists()
    assert workspace.load(requirement_id)["meta"]["status"] == "draft"
