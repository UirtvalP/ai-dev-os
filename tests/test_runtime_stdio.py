"""真正启动隔离的假进程验证 stdio 行为，不使用网络或模型。"""

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from workspace_orchestrator.agent_runtime import stdio as stdio_module
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


def _windows_test_process_api():
    """测试只操作自己创建并持有真实句柄的进程，不扫描或终止其他服务。"""

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 挂起启动竞态")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_windows_spawn_on_start_is_in_job_before_target_code_runs(
    tmp_path, monkeypatch, parent_exits
):
    marker = tmp_path / "owned-child-pid"
    seen = []
    ready = threading.Event()
    closed = threading.Event()
    observed_before_assignment = []
    original_init = stdio_module._ProcessTree.__init__

    def delayed_assignment(tree, process):
        # 精确放大旧实现 Popen 到 AssignProcessToJobObject 之间的逃逸窗口。
        time.sleep(.35)
        observed_before_assignment.append(marker.exists())
        original_init(tree, process)

    def receive(message):
        seen.append(message)
        ready.set()

    monkeypatch.setattr(stdio_module._ProcessTree, "__init__", delayed_assignment)
    client = server(f"""
import json,subprocess,sys
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],
                       stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr)
Path({str(marker)!r}).write_text(str(child.pid),encoding='utf-8')
print(json.dumps({{'type':'child','pid':child.pid}}),flush=True)
{"" if parent_exits else "sys.stdin.read()"}
""", raw_mode=True, on_message=receive)
    kernel = _windows_test_process_api()
    child_handle = None
    closer = None

    def close_client():
        try:
            client.close()
        finally:
            closed.set()

    try:
        client.start()
        assert ready.wait(5)
        # SYNCHRONIZE | PROCESS_TERMINATE，仅保留本测试子进程的精确句柄。
        child_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, seen[0]["pid"])
        assert child_handle
        assert kernel.WaitForSingleObject(child_handle, 0) == 258  # WAIT_TIMEOUT，子进程确实活着。
        closer = threading.Thread(target=close_client, daemon=True)
        closer.start()
        assert closed.wait(3), "关闭 Runtime 被逃逸后代持有的 pipe 阻塞"
        assert observed_before_assignment == [False], "目标代码在加入 Job 前已经执行"
        assert kernel.WaitForSingleObject(child_handle, 1000) == 0
        assert all(not thread.is_alive() for thread in (
            client._reader, client._stderr_reader, client._writer
        ))
    finally:
        # 即使在旧实现上复现失败，也先回收自己创建的后代再等待 close，防止测试泄漏。
        if child_handle is None and marker.exists():
            child_handle = kernel.OpenProcess(
                0x00100000 | 0x0001, False, int(marker.read_text(encoding="utf-8"))
            )
        if child_handle:
            if kernel.WaitForSingleObject(child_handle, 0) == 258:
                kernel.TerminateProcess(child_handle, 1)
                assert kernel.WaitForSingleObject(child_handle, 5000) == 0
            kernel.CloseHandle(child_handle)
        if closer:
            closer.join(timeout=5)
            assert not closer.is_alive()
        else:
            client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 挂起启动失败清理")
@pytest.mark.parametrize(
    ("failure_point", "failure_value"),
    [
        ("CreateJobObjectW", 0),
        ("SetInformationJobObject", 0),
        ("AssignProcessToJobObject", 0),
        ("CreateToolhelp32Snapshot", -1),
        ("Thread32First", 0),
        ("Thread32Next", 0),
        ("OpenThread", 0),
        ("GetProcessIdOfThread", 0),
        ("ResumeThread", 0xFFFFFFFF),
        ("ResumeThread", 0),
        ("ResumeThread", 2),
    ],
)
def test_windows_start_failure_never_runs_target_and_closes_owned_resources(
    tmp_path, monkeypatch, failure_point, failure_value
):
    import ctypes

    marker = tmp_path / "must-not-execute"
    windows_kernel = stdio_module._windows_kernel
    created = []
    closed = []

    def fail(*args):
        ctypes.set_last_error(5)
        return ctypes.c_void_p(-1).value if failure_value == -1 else failure_value

    def failing_kernel():
        kernel = windows_kernel()
        for name in ("CreateJobObjectW", "CreateToolhelp32Snapshot", "OpenThread"):
            original = getattr(kernel, name)

            def create(*args, method=original):
                handle = method(*args)
                if handle and handle != ctypes.c_void_p(-1).value:
                    created.append(handle)
                return handle

            setattr(kernel, name, create)
        close_handle = kernel.CloseHandle

        def close(handle):
            closed.append(handle)
            return close_handle(handle)

        kernel.CloseHandle = close
        setattr(kernel, failure_point, fail)
        return kernel

    monkeypatch.setattr(stdio_module, "_windows_kernel", failing_kernel)
    client = server(f"from pathlib import Path;Path({str(marker)!r}).write_text('executed')")
    try:
        with pytest.raises(RpcTransportError) as raised:
            client.start()
        assert raised.value.code == "unavailable"
        assert client._process is not None
        assert client._process.poll() is not None
        assert all(stream.closed for stream in (
            client._process.stdin, client._process.stdout, client._process.stderr
        ))
        assert client._reader is client._stderr_reader is client._writer is None
        assert not marker.exists()
        assert sorted(created) == sorted(closed)
    finally:
        client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 挂起线程归属检查")
@pytest.mark.parametrize("matching_threads", [0, 2])
def test_windows_ambiguous_initial_thread_never_resumes(tmp_path, monkeypatch, matching_threads):
    import ctypes

    marker = tmp_path / "must-not-execute"
    windows_kernel = stdio_module._windows_kernel
    client = server(f"from pathlib import Path;Path({str(marker)!r}).write_text('executed')")
    resumed = []

    def ambiguous_kernel():
        kernel = windows_kernel()
        count = 0

        def next_entry(snapshot, pointer):
            nonlocal count
            if count >= matching_threads:
                ctypes.set_last_error(18)
                return 0
            entry = ctypes.cast(pointer, ctypes.POINTER(stdio_module._ThreadEntry)).contents
            entry.dwSize = ctypes.sizeof(entry)
            entry.th32OwnerProcessID = client.pid
            entry.th32ThreadID = count + 1
            count += 1
            return 1

        kernel.Thread32First = kernel.Thread32Next = next_entry
        kernel.ResumeThread = lambda handle: resumed.append(handle)
        return kernel

    monkeypatch.setattr(stdio_module, "_windows_kernel", ambiguous_kernel)
    try:
        with pytest.raises(RpcTransportError, match="唯一"):
            client.start()
        assert not resumed
        assert not marker.exists()
        assert client._process.poll() is not None
    finally:
        client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 恢复前异常清理")
def test_windows_unexpected_resume_exception_still_reaps_suspended_process(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-execute"

    def interrupted_resume(tree):
        raise KeyboardInterrupt("模拟恢复前控制器中断")

    monkeypatch.setattr(stdio_module._ProcessTree, "_resume_suspended", interrupted_resume)
    client = server(f"from pathlib import Path;Path({str(marker)!r}).write_text('executed')")
    try:
        with pytest.raises(KeyboardInterrupt):
            client.start()
        assert client._process.poll() is not None
        assert not marker.exists()
        assert all(stream.closed for stream in (
            client._process.stdin, client._process.stdout, client._process.stderr
        ))
    finally:
        client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job 关闭回收策略")
def test_windows_job_kills_owned_process_when_its_last_handle_closes():
    import ctypes
    from ctypes import wintypes

    with server("import sys;sys.stdin.read()") as client:
        tree = client._tree
        kernel = tree._kernel
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        limits = stdio_module._JobExtendedLimits()
        assert kernel.QueryInformationJobObject(
            tree._job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None
        )
        assert limits.BasicLimitInformation.LimitFlags == 0x2000
        # 只设 KILL_ON_JOB_CLOSE，未开放 BREAKAWAY_OK 或 SILENT_BREAKAWAY_OK。
        assert client._process.poll() is None
        assert kernel.CloseHandle(tree._job)
        tree._job = None
        assert client._process.wait(timeout=5) is not None
