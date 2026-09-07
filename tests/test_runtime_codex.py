"""Codex App Server JSONL 假进程端到端合同测试。"""

import os
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from workspace_orchestrator.adapters.agent import (
    AgentProviderError,
    CodexAgentProvider,
    CodexExecutionResult,
    _archive_via_app_server,
)
from workspace_orchestrator.agent_runtime.codex import CodexRuntime, codex_command
from workspace_orchestrator.agent_runtime.contracts import AgentRunRequest, AgentRunResult

FAKE = r'''
import json,sys,threading,time,os
send_lock=threading.Lock()
def send(message):
    with send_lock: print(json.dumps(message),flush=True)
def reply(req,result): send({'id':req['id'],'result':result})
def error(req,message,code=-32600):
    send({'id':req['id'],'error':{'code':code,'message':message}})
def notify(method,params): send({'method':method,'params':params})
initialized=False; acknowledged=False; thread_id='owned-thread'; count=0
current_turn=None; approvals={}; trace=[]
for line in sys.stdin:
    req=json.loads(line)
    if 'method' not in req:
        original=approvals.pop(req['id'])
        notify('test/approvalResponse',{'threadId':thread_id,'response':req})
        notify('turn/completed',{'threadId':thread_id,'turn':{
            'id':original,'status':'completed','items':[]}})
        continue
    method=req['method']; p=req.get('params',{}); trace.append(method)
    if method=='initialize':
        assert not initialized
        assert p['clientInfo']['name']=='ai_dev_os'
        initialized=True
        time.sleep(.015)
        reply(req,{'userAgent':'codex/0.141.0'})
        continue
    if method=='initialized':
        assert initialized
        acknowledged=True;continue
    assert acknowledged, '请求必须等待初始化响应与initialized'
    if method=='model/list':
        reply(req,{'data':[{'id':'model-a','model':'actual-model','displayName':'Actual',
            'supportedReasoningEfforts':[{'reasoningEffort':'ultra'}],
            'isDefault':True,'future':{'unchanged':3}}], 'nextCursor':None})
    elif method in ('thread/start','thread/resume'):
        if method=='thread/resume' and p['threadId']=='missing':
            error(req,'no rollout found for thread id missing');continue
        if method=='thread/resume' and p['threadId']=='network':
            error(req,'transport not found while loading thread');continue
        assert p['approvalPolicy']=='on-request' and p['approvalsReviewer']=='user'
        assert p['sandbox']=='read-only'
        reply(req,{'thread':{'id':thread_id,'cwd':p['cwd']}})
        notify('thread/started',{'thread':{'id':thread_id}})
    elif method=='turn/start':
        count+=1; current_turn='turn-'+str(count)
        text=p['input'][0]['text']
        notify('turn/started',{'threadId':thread_id,'turn':{'id':current_turn}})
        # 部分服务器先推送completion，再返回turn/start响应，消费者必须仍可回放。
        if text=='eof': sys.exit(0)
        if text=='turn-missing':
            error(req,'no rollout found for thread id owned-thread');continue
        if text=='approval':
            approvals[42]=current_turn
            reply(req,{'turn':{'id':current_turn}})
            send({'id':42,'method':'item/commandExecution/requestApproval','params':{
                'threadId':thread_id,'turnId':current_turn,
                'availableDecisions':['accept','decline']}})
            continue
        if text not in ('hold','foreign'):
            notify('item/agentMessage/delta',{'threadId':thread_id,'turnId':current_turn,'delta':'你'})
            notify('item/agentMessage/delta',{'threadId':thread_id,'turnId':current_turn,'delta':'好'})
            notify('item/completed',{'threadId':thread_id,'turnId':current_turn,
                'item':{'type':'agentMessage','id':'msg','text':'你好'}})
            notify('future/unknown',{'threadId':thread_id,'turnId':current_turn,
                'opaque':{'nested':[1,'two']}})
            notify('turn/completed',{'threadId':thread_id,'turn':{
                'id':current_turn,'status':'failed' if text=='fail' else 'completed'}})
        if text=='foreign':
            notify('turn/completed',{'threadId':'not-owned','turn':{
                'id':current_turn,'status':'completed'}})
        reply(req,{'turn':{'id':current_turn,'status':'inProgress'}})
    elif method=='turn/steer':
        assert p['expectedTurnId']==current_turn
        reply(req,{'turnId':current_turn})
    elif method=='turn/interrupt':
        assert p['turnId']==current_turn
        reply(req,{})
        notify('turn/completed',{'threadId':thread_id,'turn':{
            'id':current_turn,'status':'interrupted'}})
    elif method=='thread/read':
        reply(req,{'thread':{'id':thread_id},'trace':trace,
            'leaked_thread_id':os.environ.get('CODEX_THREAD_ID')})
    elif method=='thread/archive':
        assert p['threadId'] in ('owned-thread','archive-target')
        # 此响应只有stdin仍开启时才可收到，旧subprocess.run会在初始化之前关闭它。
        time.sleep(.02);reply(req,{})
        notify('thread/archived',{'threadId':p['threadId']})
    else: error(req,'unsupported',-32601)
'''


def runtime(**kwargs):
    return CodexRuntime(command=[sys.executable, "-u", "-c", FAKE], **kwargs)


def request(tmp_path, prompt="normal", **kwargs):
    return AgentRunRequest("run-test", tmp_path, prompt, sandbox="read-only", **kwargs)


def test_models_start_message_stream_archive_and_raw_unknown_events(tmp_path):
    events = []
    provider = runtime(event_sink=events.append)
    try:
        descriptor = provider.describe()
        assert descriptor.available
        assert descriptor.supports("steer")
        assert descriptor.models[0].id == "actual-model"
        assert descriptor.models[0].reasoning_efforts == ("ultra",)
        assert descriptor.models[0].metadata["future"] == {"unchanged": 3}
        started = provider.start(request(tmp_path))
        assert started.ok and started.session and started.turn_id
        result = provider.wait(started.session, started.turn_id, timeout_seconds=3)
        assert result.returncode == 0 and result.summary == "你好"
        assert result.runtime_id == "codex" and result.run_id == "run-test"
        assert provider._client.running
        assert all(event.run_id == "run-test" for event in events)
        unknown = next(event for event in events if event.payload["method"] == "future/unknown")
        assert unknown.kind == "unknown"
        assert unknown.payload["params"]["opaque"] == {"nested": [1, "two"]}
        second = provider.send_message(started.session, "next")
        assert second.ok and second.turn_id != started.turn_id
        assert provider.wait(started.session, second.turn_id, timeout_seconds=3).returncode == 0
        read = provider.read_session(started.session)
        assert read.ok and read.data["leaked_thread_id"] is None
        assert provider.archive(started.session).ok
    finally:
        provider.close()


def test_resume_missing_is_distinct_from_unknown_failure_and_can_fallback(tmp_path):
    provider = runtime()
    try:
        missing = provider.resume(request(tmp_path, resume_session_id="missing"))
        assert missing.error.code == "session_missing"
        started = provider.start(request(tmp_path))
        assert started.ok
    finally:
        provider.close()
    provider = runtime()
    try:
        result = provider.resume(request(tmp_path, resume_session_id="network"))
        assert not result.ok and result.error.code == "rpc_error"
        assert provider._session is None
    finally:
        provider.close()
    provider = runtime()
    try:
        started = provider.resume(request(tmp_path, resume_session_id="owned-thread"))
        result = provider.wait(started.session, started.turn_id, timeout_seconds=3)
        assert result.returncode == 0 and result.resumed
    finally:
        provider.close()


def test_steer_interrupt_and_scope_mismatches(tmp_path):
    provider = runtime()
    try:
        started = provider.start(request(tmp_path, "hold"))
        assert provider.steer(started.session, started.turn_id, "more").ok
        foreign = replace(started.session, run_id="other")
        for operation in (
            provider.send_message(foreign, "bad"), provider.archive(foreign),
            provider.read_session(foreign), provider.interrupt(foreign, started.turn_id),
            provider.steer(started.session, "other-turn", "bad"),
        ):
            assert operation.error.code == "scope_mismatch"
            assert operation.session is None
        assert provider.interrupt(started.session, started.turn_id).ok
        result = provider.wait(started.session, started.turn_id, timeout_seconds=3)
        assert result.returncode == 130 and result.error.code == "interrupted"
    finally:
        provider.close()


@pytest.mark.parametrize("prompt,expected", [("fail", "turn_failed"), ("hold", "timeout"),
                                               ("foreign", "timeout")])
def test_non_success_cannot_become_candidate_complete(tmp_path, prompt, expected):
    provider = runtime()
    try:
        started = provider.start(request(tmp_path, prompt))
        result = provider.wait(started.session, started.turn_id, timeout_seconds=.1)
        assert result.returncode != 0 and result.error.code == expected
    finally:
        provider.close()


@pytest.mark.parametrize("resumed", [False, True])
def test_eof_during_turn_start_retains_confirmed_session_identity(tmp_path, resumed):
    provider = runtime()
    try:
        operation = provider.resume if resumed else provider.start
        result = operation(request(tmp_path, "eof", resume_session_id="owned-thread" if resumed else None))
        assert not result.ok and result.error.code == "eof"
        assert result.session.session_id == "owned-thread"
        assert result.session.runtime_id == "codex"
        assert result.session.run_id == "run-test"
        assert result.session.workspace_path == str(tmp_path.resolve())
    finally:
        provider.close()


def test_missing_error_after_resume_confirmation_does_not_allow_prompt_replay(tmp_path):
    provider = runtime()
    try:
        result = provider.resume(request(tmp_path, "turn-missing", resume_session_id="owned-thread"))
        assert not result.ok and result.error.code == "rpc_error"
        assert result.session.session_id == "owned-thread"
    finally:
        provider.close()


def test_approval_default_declines_and_defer_requires_explicit_scoped_decision(tmp_path):
    events = []
    provider = runtime(event_sink=events.append)
    try:
        started = provider.start(request(tmp_path, "approval"))
        assert provider.wait(started.session, started.turn_id, timeout_seconds=3).returncode == 0
        response = next(e for e in events if e.payload["method"] == "test/approvalResponse")
        assert response.payload["params"]["response"]["result"] == {"decision": "decline"}
    finally:
        provider.close()
    ready = threading.Event()
    provider = runtime(event_sink=lambda e: ready.set() if e.kind == "approval" else None,
                       approval_mode="defer")
    try:
        started = provider.start(request(tmp_path, "approval"))
        assert ready.wait(3)
        foreign = replace(started.session, session_id="other")
        assert not provider.respond_to_request(foreign, 42, {"decision": "accept"}).ok
        assert not provider.respond_to_request(started.session, 42, {"decision": "cancel"}).ok
        assert provider.respond_to_request(started.session, 42, {"decision": "decline"}).ok
        assert not provider.respond_to_request(started.session, 42, {"decision": "accept"}).ok
        assert provider.wait(started.session, started.turn_id, timeout_seconds=3).returncode == 0
    finally:
        provider.close()


def test_failed_event_persistence_cannot_publish_success(tmp_path):
    def fail_completion(event):
        if event.kind == "completion":
            raise ValueError("模拟事件磁盘失败")

    provider = runtime(event_sink=fail_completion)
    try:
        started = provider.start(request(tmp_path))
        if started.ok:
            result = provider.wait(started.session, started.turn_id, timeout_seconds=3)
            assert result.returncode != 0
        else:
            assert started.error.code == "protocol_error"
    finally:
        provider.close()


def test_missing_runtime_is_truthfully_unavailable(tmp_path):
    provider = CodexRuntime(executable="ai-dev-os-missing-codex-928918")
    try:
        assert not provider.describe().available
        assert provider.start(request(tmp_path)).status == "unavailable"
    finally:
        provider.close()


def test_archive_facade_waits_for_handshake_and_matching_reply(monkeypatch):
    from workspace_orchestrator.adapters import agent

    monkeypatch.setattr(agent, "codex_command", lambda: (sys.executable, "-u", "-c", FAKE))
    _archive_via_app_server("archive-target")
    monkeypatch.setattr(agent, "codex_command", lambda: (sys.executable, "-c", "pass"))
    with pytest.raises(AgentProviderError):
        _archive_via_app_server("archive-target")
    assert CodexExecutionResult is AgentRunResult
    assert CodexExecutionResult(0, "legacy", "out", "err").resumed is False


@pytest.mark.skipif(os.environ.get("AI_DEV_OS_CODEX_LIVE_SMOKE") != "1", reason="仅显式本机 smoke")
def test_live_codex_created_thread_only(tmp_path):
    """只控制本次新建的临时 Thread，不读取/归档用户已有 Thread。"""
    activity = threading.Condition()
    started_items = set()

    def note_activity(event):
        if event.payload.get("method") == "item/started" and event.turn_id:
            with activity:
                started_items.add(event.turn_id)
                activity.notify_all()

    provider = CodexRuntime(timeout_seconds=45, event_sink=note_activity)
    created_id = None
    failures = []
    stage = "describe"
    try:
        selected_command = codex_command()
        cli_version = subprocess.run(
            [*selected_command, "--version"], check=True, text=True, encoding="utf-8",
            capture_output=True, timeout=15,
        ).stdout.strip()
        print(f"live codex executable={selected_command!r} version={cli_version}", flush=True)
        descriptor = provider.describe()
        assert descriptor.available and descriptor.models, descriptor
        print(f"live codex stage={stage} models={len(descriptor.models)}", flush=True)
        stage = "start"
        started = provider.start(request(tmp_path, "不要调用工具；只回复 RUNTIME_SMOKE_OK"))
        if started.session:
            created_id = started.session.session_id
        print(f"live codex stage={stage} result={started}", flush=True)
        assert started.ok, started
        # 归档操作针对持久 rollout；首轮尚未物化就立刻中断不能证明可恢复能力。
        stage = "first_turn_complete"
        first_result = provider.wait(started.session, started.turn_id, timeout_seconds=90)
        print(f"live codex stage={stage} result={first_result}", flush=True)
        assert first_result.returncode == 0, first_result
        assert "RUNTIME_SMOKE_OK" in first_result.summary
        stage = "read_persisted_first_turn"
        stored = provider.read_session(started.session)
        print(f"live codex stage={stage} result={stored}", flush=True)
        assert stored.ok, stored
        assert stored.data["thread"].get("ephemeral") is False
        assert any(turn["id"] == started.turn_id for turn in stored.data["thread"]["turns"])
        stage = "control_turn_start"
        controlled = provider.send_message(started.session, "不要调用工具；请等待进一步引导")
        print(f"live codex stage={stage} result={controlled}", flush=True)
        assert controlled.ok, controlled
        # turn/start 初始响应并不保证后台任务已挂载；用真实 item 事件确认进入执行。
        stage = "control_turn_item_started"
        with activity:
            assert activity.wait_for(lambda: controlled.turn_id in started_items, timeout=45)
        print(f"live codex stage={stage} turn={controlled.turn_id} PASS", flush=True)
        stage = "steer"
        steered = provider.steer(started.session, controlled.turn_id, "不要执行文件或网络操作")
        print(f"live codex stage={stage} result={steered}", flush=True)
        assert steered.ok, steered
        stage = "interrupt"
        interrupted = provider.interrupt(started.session, controlled.turn_id)
        print(f"live codex stage={stage} result={interrupted}", flush=True)
        assert interrupted.ok, interrupted
        interrupted_result = provider.wait(started.session, controlled.turn_id, timeout_seconds=45)
        print(f"live codex stage={stage}_complete result={interrupted_result}", flush=True)
        assert interrupted_result.returncode == 130, interrupted_result
        provider.close()
        provider = CodexRuntime(timeout_seconds=45)
        stage = "resume_new_connection"
        resumed = provider.resume(request(tmp_path, "不要调用工具；只回复 RUNTIME_SMOKE_OK",
                                          resume_session_id=created_id))
        print(f"live codex stage={stage} result={resumed}", flush=True)
        assert resumed.ok, resumed
        stage = "resumed_turn_complete"
        result = provider.wait(resumed.session, resumed.turn_id, timeout_seconds=90)
        print(f"live codex stage={stage} result={result}", flush=True)
        assert result.returncode == 0, result
        assert "RUNTIME_SMOKE_OK" in result.summary
    except Exception as exc:  # noqa: BLE001 -- 保留主失败，不能被 finally 归档失败覆盖。
        exc.add_note(f"live Codex 失败阶段：{stage}；本次新建 Thread：{created_id}")
        failures.append(exc)
    finally:
        try:
            provider.close()
        except Exception as exc:  # noqa: BLE001 -- 清理失败亦须令 smoke 失败。
            failures.append(exc)
        if created_id:
            try:
                CodexAgentProvider().archive_session(created_id)
                print(f"live codex stage=archive thread={created_id} PASS", flush=True)
            except Exception as exc:  # noqa: BLE001 -- 显示归档错误，而非假成功或覆盖原始错误。
                exc.add_note(f"live Codex 归档失败：{created_id}")
                failures.append(exc)
    if failures:
        raise ExceptionGroup("Codex 真实 smoke 失败（包含执行及清理错误）", failures)
    print(f"live codex thread={created_id} PASS", flush=True)
