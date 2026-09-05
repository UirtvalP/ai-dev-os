"""真正启动隔离的假进程验证 stdio 行为，不使用网络或模型。"""

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from workspace_orchestrator.agent_runtime.stdio import (
    JsonRpcStdioClient,
    RpcResponseError,
    RpcTransportError,
)


def server(source, **kwargs):
    return JsonRpcStdioClient([sys.executable, "-u", "-c", source], **kwargs)


def test_requests_match_out_of_order_responses_and_stream_before_exit():
    seen = []
    with server("""
import json,sys
send=lambda x:print(json.dumps(x),flush=True)
a=json.loads(sys.stdin.readline()); b=json.loads(sys.stdin.readline())
send({'method':'unknown/future','params':{'unicode':'你好','nested':{'x':3}}})
send({'id':b['id'],'result':{'method':b['method']}})
send({'id':a['id'],'result':{'method':a['method']}})
sys.stdin.read()
""", on_notification=seen.append, jsonrpc=True) as client:
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(client.request, "one")
            two = pool.submit(client.request, "two")
            assert one.result() == {"method": "one"}
            assert two.result() == {"method": "two"}
        assert client.running
        assert seen == [{"method": "unknown/future", "params": {
            "unicode": "你好", "nested": {"x": 3}
        }}]


def test_server_request_is_rejected_by_default_without_blocking_client_request():
    with server("""
import json,sys
send=lambda x:print(json.dumps(x),flush=True)
request=json.loads(sys.stdin.readline())
send({'id':42,'method':'permissions/request','params':{}})
response=json.loads(sys.stdin.readline())
send({'id':request['id'],'result':response})
sys.stdin.read()
""") as client:
        result = client.request("go")
        assert result["id"] == 42
        assert result["error"]["code"] == -32601
        assert "result" not in result


def test_server_request_can_be_answered_from_reader_without_deadlock():
    requests = []

    def answer(message):
        requests.append(message)
        client.respond(message["id"], {"decision": "decline"})

    with server("""
import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({'id':'approval-1','method':'approve','params':{}}),flush=True)
answer=json.loads(sys.stdin.readline())
print(json.dumps({'id':request['id'],'result':answer['result']}),flush=True)
sys.stdin.read()
""", on_server_request=answer) as client:
        assert client.request("go") == {"decision": "decline"}
        assert len(requests) == 1


def test_timeout_does_not_misroute_late_response():
    with server("""
import json,sys,time
first=json.loads(sys.stdin.readline());time.sleep(.2)
print(json.dumps({'id':first['id'],'result':{'old':True}}),flush=True)
second=json.loads(sys.stdin.readline())
print(json.dumps({'id':second['id'],'result':{'new':True}}),flush=True)
sys.stdin.read()
""") as client:
        with pytest.raises(RpcTransportError) as raised:
            client.request("slow", timeout=.05)
        assert raised.value.code == "timeout"
        assert client.request("next", timeout=3) == {"new": True}


@pytest.mark.parametrize("output", ["not-json", "[]", '{"id":"client-1"}', '{"id":false}'])
def test_invalid_wire_fails_all_pending_without_hanging(output):
    with server(f"import sys; sys.stdin.readline(); print({output!r},flush=True)") as client:
        with pytest.raises(RpcTransportError) as raised:
            client.request("go", timeout=3)
        assert raised.value.code == "protocol_error"


def test_eof_never_counts_as_success_and_remote_errors_preserve_details():
    with server("import sys;sys.stdin.readline()") as client:
        with pytest.raises(RpcTransportError) as raised:
            client.request("go", timeout=3)
        assert raised.value.code == "eof"
    with server("""
import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({'id':request['id'],'error':{
 'code':-32042,'message':'denied','data':{'scope':'thread'}}}),flush=True)
sys.stdin.read()
""") as client:
        with pytest.raises(RpcResponseError) as raised:
            client.request("go")
        assert raised.value.code == -32042
        assert raised.value.data == {"scope": "thread"}
        assert client.running


def test_raw_jsonl_reuses_lifecycle_and_notifies_failure():
    seen = []
    failures = []
    ready = threading.Event()

    def receive(message):
        seen.append(message)
        ready.set()

    with server("""
import sys
print(sys.stdin.readline().strip(),flush=True)
""", raw_mode=True, on_message=receive, on_error=failures.append) as client:
        client.send({"type": "control_request", "request_id": "x"})
        assert ready.wait(3)
        assert seen == [{"type": "control_request", "request_id": "x"}]
        with pytest.raises(RpcTransportError, match="原始"):
            client.request("no")
    assert len(failures) == 1
    assert client.failure is failures[0]


def test_null_result_compatibility_and_close_wakes_waiter():
    with server("""
import sys,json
request=json.loads(sys.stdin.readline())
print(json.dumps({'id':request['id'],'result':None}),flush=True)
sys.stdin.read()
""") as client:
        assert client.request("load") == {}
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(client.request, "hang", timeout=30)
            client.close()
            with pytest.raises(RpcTransportError):
                waiting.result(timeout=3)
        client.close()


def test_missing_executable_reports_unavailable():
    client = JsonRpcStdioClient(["ai-dev-os-no-such-runtime-9081827"])
    with pytest.raises(RpcTransportError) as raised:
        client.start()
    assert raised.value.code == "unavailable"
    client.close()


def test_stdin_backpressure_does_not_disable_request_timeout():
    with server("import time;time.sleep(120)") as client:
        started = time.monotonic()
        with pytest.raises(RpcTransportError) as raised:
            client.request("large", {"text": "x" * 1024 * 1024}, timeout=.1)
        assert raised.value.code == "timeout"
        assert time.monotonic() - started < 3


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 批处理 shell 边界")
@pytest.mark.parametrize("name", ["fake.cmd", "fake.bat", "fake.cmd ", "fake.cmd::$DATA"])
def test_windows_implicit_batch_execution_is_rejected(name):
    client = JsonRpcStdioClient([name, "untrusted & argument"])
    with pytest.raises(RpcTransportError) as raised:
        client.start()
    assert raised.value.code == "unavailable"
    client.close()


def test_close_reaps_spawned_descendant_even_after_parent_exit():
    seen = []
    ready = threading.Event()

    def receive(message):
        seen.append(message)
        ready.set()

    client = server("""
import sys,subprocess,json
sys.stdin.readline()
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
print(json.dumps({'type':'child','pid':child.pid}),flush=True)
""", raw_mode=True, on_message=receive)
    client.start()
    client.send({"type": "go"})
    assert ready.wait(3)
    child_pid = seen[0]["pid"]
    client.close()
    assert not client.running
    if sys.platform == "win32":
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"], text=True
        )
        assert f'"{child_pid}"' not in output
    else:
        # 容器 init 可能延迟回收孤儿 zombie，但不能留下可执行的后代。
        from pathlib import Path
        for _ in range(100):
            path = Path(f"/proc/{child_pid}/stat")
            if not path.exists() or path.read_text().split()[2] == "Z":
                break
            time.sleep(.01)
        else:
            os.kill(child_pid, 9)
            pytest.fail("Runtime 后代进程未回收")
