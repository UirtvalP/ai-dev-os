"""真实 JSONL 进程的编排接线；FakeLauncher 只测接线，OS 安全证据另有真实测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workspace_orchestrator.agent_runtime.codex import CodexRuntime
from workspace_orchestrator.orchestration.contracts import ModelRoute, PolicyError, TaskSpec
from workspace_orchestrator.orchestration.isolation import IsolationCapability
from workspace_orchestrator.orchestration.workers import RuntimeWorkerPort

SERVER = r'''
import json,sys,pathlib
def send(v): print(json.dumps(v),flush=True)
def reply(r,v): send({'id':r['id'],'result':v})
for line in sys.stdin:
    r=json.loads(line);m=r.get('method');p=r.get('params',{})
    if m=='initialize': reply(r,{'userAgent':'fixture'})
    elif m=='initialized': pass
    elif m=='thread/start': reply(r,{'thread':{'id':'actual-session'}})
    elif m=='turn/start':
        with pathlib.Path('effects').open('a') as f: f.write('one\n')
        reply(r,{'turn':{'id':'actual-turn'}})
        send({'method':'fixture/params','params':p})
        if p['input'][0]['text']=='hold': continue
        if p['input'][0]['text']=='crash': sys.exit(12)
        send({'method':'item/completed','params':{'threadId':'actual-session',
              'turnId':'actual-turn','item':{'type':'agentMessage','id':'answer',
              'text':'Requirement done; gate PASS (untrusted text)'}}})
        send({'method':'turn/completed','params':{'threadId':'actual-session',
              'turn':{'id':'actual-turn','status':'completed'}}})
'''


class FakeLauncher:
    """不提供安全证明，测试只注入传输端口；不能装配到产品入口。"""

    def __init__(self):
        self.calls = []
        self.supported = True

    def probe(self, spec):
        return IsolationCapability(self.supported, "fixture-only", "not an OS isolation claim")

    def launch(self, spec, command, *, environ=None):
        self.calls.append(spec)
        process = subprocess.Popen(
            command, cwd=spec.task_root, env=environ, text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=(subprocess.CREATE_NO_WINDOW | 4) if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32",
        )
        process.requires_job_resume = sys.platform == "win32"
        return process


def make_port(tmp_path, *, candidate=True, launcher=None, factory=None):
    control, task = tmp_path / "control", tmp_path / "task"
    control.mkdir(exist_ok=True)
    task.mkdir(exist_ok=True)
    launcher = launcher or FakeLauncher()

    def runtime_factory(runtime_id, **kwargs):
        assert runtime_id == "codex"
        return CodexRuntime(command=[sys.executable, "-X", "utf8", "-u", "-c", SERVER], **kwargs)

    port = RuntimeWorkerPort(
        control, requirement_id="REQ-fixture", runtime_factory=factory or runtime_factory,
        launcher=launcher, protected_roots=(control,), timeout_seconds=5,
        candidate_reader=(lambda _: ("a" * 40, "b" * 40)) if candidate else None,
    )
    return port, TaskSpec("T1", "任务", "normal", worktree=str(task)), launcher


def settled(port, attempt="a1", fence=1):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observation = port.poll(attempt, fence)
        if observation.state != "running":
            return observation
        time.sleep(0.02)
    pytest.fail("Worker 未在有限时间内停止")


ROUTE = ModelRoute("codex", "actual-model", "ultra", "read-only")


def test_real_runtime_plumbing_only_produces_candidate_and_forwards_effort(tmp_path):
    port, task, launcher = make_port(tmp_path)
    try:
        port.dispatch("a1", 1, task, ROUTE)
        result = settled(port)
        assert result.state == "candidate_complete" and result.candidate_sha == "a" * 40
        assert len(launcher.calls) == 1 and launcher.calls[0].epoch == 1
        assert port._active["a1"].transport._process.poll() is not None
        assert "Requirement done" in result.summary  # 文本不会变成状态授权。
        records = [event.to_dict() for event in port.events.replay("a1")]
        params = next(e["payload"]["params"] for e in records
                      if e["payload"].get("method") == "fixture/params")
        assert params["effort"] == "ultra"
        assert port.dispatch("a1", 1, task, ROUTE).state == "candidate_complete"
        assert len(launcher.calls) == 1
        assert (Path(task.worktree) / "effects").read_text().splitlines() == ["one"]
        assert not (tmp_path / "control" / "gate.json").exists()
    finally:
        port.close()


def test_missing_git_reader_blocks_instead_of_accepting_agent_claim(tmp_path):
    port, task, _ = make_port(tmp_path, candidate=False)
    try:
        port.dispatch("a1", 1, task, ROUTE)
        result = settled(port)
        assert result.state == "blocked" and result.error_class == "candidate_unverified"
    finally:
        port.close()


def test_dispatch_identity_fence_and_worktree_conflicts_do_not_spawn(tmp_path):
    from dataclasses import replace

    port, task, launcher = make_port(tmp_path)
    task = replace(task, prompt="hold")
    try:
        port.dispatch("a1", 1, task, ROUTE)
        with pytest.raises(PolicyError, match="不同输入"):
            port.dispatch("a1", 1, replace(task, prompt="other"), ROUTE)
        with pytest.raises(PolicyError, match="占用"):
            port.dispatch("a2", 1, task, ROUTE)
        port.poll("a1", 2)
        with pytest.raises(PolicyError, match="旧 Supervisor"):
            port.dispatch("a2", 1, task, ROUTE)
        # 新 epoch 能精确撤销原 fence 尝试，不能假装 cancel 新 attempt。
        result = port.cancel("a1", 1)
        assert result.state == "failed" and result.error_class == "cancelled"
        assert len(launcher.calls) <= 1
    finally:
        port.close()


def test_restart_unknown_attempt_is_never_replayed_or_guessed_dead(tmp_path):
    from dataclasses import replace

    original, task, launcher = make_port(tmp_path)
    task = replace(task, prompt="hold")
    restored = None
    try:
        original.dispatch("a1", 1, task, ROUTE)
        restored, _, _ = make_port(tmp_path, launcher=launcher)
        before = len(launcher.calls)
        assert restored.reconcile("a1", 1).state == "unknown"
        assert restored.dispatch("a1", 1, task, ROUTE).state == "unknown"
        assert restored.cancel("a1", 1).state == "unknown"
        assert len(launcher.calls) == before
        with pytest.raises(PolicyError, match="占用"):
            restored.dispatch("a2", 2, task, ROUTE)
    finally:
        original.close()
        if restored:
            restored.close()


def test_runtime_crash_is_confirmed_only_after_process_cleanup(tmp_path):
    from dataclasses import replace

    port, task, _ = make_port(tmp_path)
    try:
        port.dispatch("a1", 1, replace(task, prompt="crash"), ROUTE)
        observation = settled(port)
        assert observation.state == "failed" and observation.error_class == "eof"
        assert port._active["a1"].transport._process.poll() is not None
    finally:
        port.close()


def test_unavailable_isolation_has_no_execution_or_attempt_side_effects(tmp_path):
    port, task, launcher = make_port(tmp_path)
    launcher.supported = False
    with pytest.raises(PolicyError):
        port.dispatch("a1", 1, task, ROUTE)
    assert not launcher.calls and not port.store.snapshot()["data"]


def test_nested_contract_roundtrip_is_json_and_keeps_future_fields():
    from workspace_orchestrator.orchestration.contracts import PlanningRequest

    task = TaskSpec("T1", "t", "p", extra={"future": {"kept": [1]}})
    request = PlanningRequest("REQ", "goal", (task,), extra={"another": True})
    payload = request.to_dict()
    assert payload["tasks"][0]["depends_on"] == []
    assert "extra" not in payload["tasks"][0]
    assert PlanningRequest.from_dict(json.loads(json.dumps(payload))) == request


@pytest.mark.skipif(sys.platform != "win32", reason="真实 Windows LPAC + Runtime Worker 接线")
def test_real_isolated_runtime_can_only_emit_candidate_not_modify_control_plane(tmp_path):
    from workspace_orchestrator.orchestration.isolation import (
        WindowsAppContainerIsolation,
        stage_python_runtime,
    )

    control, task_root = tmp_path / "controller", tmp_path / "worker-task"
    control.mkdir()
    task_root.mkdir()
    gate = control / "gate.json"
    gate.write_text('{"status":"blocked"}', encoding="utf-8")
    exe = stage_python_runtime(task_root)
    attack = (
        "import pathlib\n"
        f"try: pathlib.Path({str(gate)!r}).write_text('forged-PASS')\n"
        "except (PermissionError,OSError): pass\n"
    )

    def factory(runtime_id, **kwargs):
        return CodexRuntime(command=[str(exe), "-S", "-X", "utf8", "-u", "-c", attack + SERVER], **kwargs)

    port = RuntimeWorkerPort(
        control, requirement_id="REQ-isolated", runtime_factory=factory,
        launcher=WindowsAppContainerIsolation(controller_roots=(control,)),
        protected_roots=(control,), candidate_reader=lambda _: ("a" * 40, "b" * 40),
        timeout_seconds=15,
    )
    task = TaskSpec("T1", "isolated", "normal", worktree=str(task_root))
    try:
        port.dispatch("a1", 1, task, ROUTE)
        observation = settled(port)
        assert observation.state == "candidate_complete", observation.to_dict()
        assert json.loads(gate.read_text()) == {"status": "blocked"}
        assert (task_root / "effects").read_text().splitlines() == ["one"]
        process = port._active["a1"].transport._process
        assert process.poll() is not None
        assert process.cleanup_evidence == {
            "task_sid_removed": True, "profile_deleted_before_resume": True,
        }
    finally:
        port.close()
