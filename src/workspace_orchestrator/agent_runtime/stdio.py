"""可并发的长期 JSONL JSON-RPC 连接，不在传输层自动授予权限。"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

JsonObject = dict[str, Any]
MessageHandler = Callable[[JsonObject], None]


class RpcTransportError(RuntimeError):
    """连接不可用、协议损坏、超时或 EOF；code 是稳定的机器可读分类。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RpcResponseError(RuntimeError):
    """服务器针对特定请求返回的错误，不等于连接已经断开。"""

    def __init__(self, error: JsonObject) -> None:
        super().__init__(str(error.get("message", "JSON-RPC 请求失败")))
        self.code = error.get("code")
        self.data = error.get("data")
        self.error = error


@dataclass(slots=True)
class _Pending:
    ready: threading.Event = field(default_factory=threading.Event)
    response: JsonObject | None = None
    error: RpcTransportError | None = None


class _ProcessTree:
    """POSIX 独立进程组；Windows Job Object 随连接回收所有后代。"""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._job: Any = None
        self._kernel: Any = None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateJobObject.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            job = kernel.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "无法创建 Runtime 进程 Job")
            if not kernel.AssignProcessToJobObject(job, int(cast(Any, process)._handle)):
                error = ctypes.get_last_error()
                kernel.CloseHandle(job)
                raise OSError(error, "无法隔离 Runtime 进程树")
            self._job, self._kernel = job, kernel

    def kill(self) -> None:
        if sys.platform == "win32":
            if self._job is not None:
                self._kernel.TerminateJobObject(self._job, 1)
                self._kernel.CloseHandle(self._job)
                self._job = None
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class JsonRpcStdioClient:
    """一个实例对应一个进程、一个长期连接；关闭后不得复用。

    回调在 stdout reader 中顺序执行，只能做轻量处理或 respond，不能等待 request。
    request 可由任意其他线程并发调用；超时只结束该等待，不伪称服务器取消执行。
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
        on_notification: MessageHandler | None = None,
        on_server_request: MessageHandler | None = None,
        jsonrpc: bool = False,
        raw_mode: bool = False,
        on_message: MessageHandler | None = None,
        on_error: Callable[[RpcTransportError], None] | None = None,
    ) -> None:
        if not command:
            raise ValueError("Runtime 命令不能为空")
        self.command = tuple(command)
        self.cwd = cwd
        self.environ = dict(environ) if environ is not None else None
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.jsonrpc = jsonrpc
        self.raw_mode = raw_mode
        self.on_message = on_message
        self.on_error = on_error
        self._process: subprocess.Popen[str] | None = None
        self._tree: _ProcessTree | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._outbound: queue.Queue[str | None] = queue.Queue(maxsize=256)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._next_id = 0
        self._failure: RpcTransportError | None = None
        self._closed = False
        self._stderr: deque[str] = deque(maxlen=128)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def running(self) -> bool:
        return bool(self._process and self._process.poll() is None and not self._failure)

    @property
    def failure(self) -> RpcTransportError | None:
        return self._failure

    @property
    def stderr_tail(self) -> str:
        with self._lock:
            return "".join(self._stderr)

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RpcTransportError("closed", "Runtime 连接已关闭")
            if self._process is not None:
                return
            try:
                if sys.platform == "win32":
                    creationflags = subprocess.CREATE_NO_WINDOW
                    executable = shutil.which(self.command[0]) or self.command[0]
                    if Path(executable).suffix.lower() not in {".exe", ".com"}:
                        raise OSError("Windows Runtime 必须使用原生 .exe/.com；禁止隐式批处理 shell")
                else:
                    creationflags = 0
                    executable = self.command[0]
                process = subprocess.Popen(
                    (executable, *self.command[1:]),
                    cwd=self.cwd,
                    env=self.environ,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    bufsize=1,
                    start_new_session=sys.platform != "win32",
                    creationflags=creationflags,
                )
                self._process = process
                self._tree = _ProcessTree(process)
            except OSError as exc:
                if self._process is not None:
                    self._process.kill()
                    self._process.wait(timeout=5)
                    for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                        if stream:
                            stream.close()
                self._failure = RpcTransportError("unavailable", f"Runtime 启动失败：{exc}")
                raise self._failure from exc
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
            self._writer = threading.Thread(target=self._write_loop, daemon=True)
            self._reader.start()
            self._stderr_reader.start()
            self._writer.start()

    def _send(self, message: JsonObject) -> None:
        if self.jsonrpc:
            message = {"jsonrpc": "2.0", **message}
        line = json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n"
        if len(line) > 8 * 1024 * 1024:
            raise RpcTransportError("protocol_error", "Runtime 消息超出 8 MiB 限制")
        with self._write_lock:
            with self._lock:
                if self._failure:
                    raise self._failure
                process = self._process
                if self._closed or process is None or process.stdin is None:
                    raise RpcTransportError("closed", "Runtime 连接尚未启动或已关闭")
            try:
                self._outbound.put_nowait(line)
            except queue.Full as exc:
                raise RpcTransportError("overloaded", "Runtime 待发送消息队列已满") from exc

    def _write_loop(self) -> None:
        """隔离管道背压，防止服务器停止读 stdin 后 request 超时也永远无法返回。"""
        assert self._process and self._process.stdin
        try:
            while True:
                line = self._outbound.get()
                if line is None:
                    return
                self._process.stdin.write(line)
                self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._fail(RpcTransportError("closed", f"Runtime 写入失败：{exc}"))

    def send(self, message: JsonObject) -> None:
        """原始 JSONL 写入入口，供非 JSON-RPC 的 stdio 协议复用生命周期。"""
        self._send(message)

    def request(
        self, method: str, params: JsonObject | None = None, *, timeout: float = 30
    ) -> JsonObject:
        if timeout <= 0:
            raise ValueError("请求 timeout 必须大于零")
        if self.raw_mode:
            raise RpcTransportError("protocol_error", "原始 JSONL 模式不能发送 JSON-RPC request")
        if threading.current_thread() is self._reader:
            raise RpcTransportError("protocol_error", "reader 回调不可同步等待另一请求")
        pending = _Pending()
        with self._lock:
            self._next_id += 1
            request_id = f"client-{self._next_id}"
            self._pending[request_id] = pending
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
            if not pending.ready.wait(timeout):
                raise RpcTransportError("timeout", f"Runtime 请求 {method} 超时（{timeout:g} 秒）")
            if pending.error:
                raise pending.error
            assert pending.response is not None
            response = pending.response
            if "error" in response:
                if not isinstance(response["error"], dict):
                    raise RpcTransportError("protocol_error", "JSON-RPC error 必须是对象")
                raise RpcResponseError(response["error"])
            result = response.get("result")
            if result is None:
                return {}
            if not isinstance(result, dict):
                raise RpcTransportError("protocol_error", "JSON-RPC result 必须是对象")
            return result
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: JsonObject | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def respond(
        self,
        request_id: int | str,
        result: JsonObject | None = None,
        *,
        error: JsonObject | None = None,
    ) -> None:
        if result is not None and error is not None:
            raise ValueError("响应不得同时包含 result 和 error")
        self._send({"id": request_id, **({"error": error} if error else {"result": result or {}})})

    def _fail(self, error: RpcTransportError) -> None:
        with self._lock:
            first_failure = self._failure is None
            if self._failure is None:
                self._failure = error
            for pending in self._pending.values():
                if not pending.ready.is_set():
                    pending.error = self._failure
                    pending.ready.set()
        if first_failure and self.on_error:
            self.on_error(error)

    def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        try:
            while line := self._process.stdout.readline(8 * 1024 * 1024 + 1):
                if len(line) > 8 * 1024 * 1024 or not line.endswith("\n"):
                    raise ValueError("JSONL 消息过长或缺少换行终止符")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise TypeError("JSON-RPC 消息必须是对象")
                if self.raw_mode:
                    if self.on_message:
                        self.on_message(message)
                    continue
                if "method" in message:
                    if not isinstance(message["method"], str):
                        raise ValueError("JSON-RPC method 必须是字符串")
                    if "id" in message:
                        if type(message["id"]) not in (int, str):
                            raise ValueError("服务端请求 ID 必须是字符串或整数")
                        if self.on_server_request:
                            self.on_server_request(message)
                        else:
                            self.respond(message["id"], error={
                                "code": -32601, "message": "客户端未提供此请求处理器"
                            })
                    elif self.on_notification:
                        self.on_notification(message)
                elif isinstance(message.get("id"), str):
                    if ("result" in message) == ("error" in message):
                        raise ValueError("JSON-RPC 响应必须恰好包含 result 或 error")
                    with self._lock:
                        pending = self._pending.get(message["id"])
                        if pending and not pending.ready.is_set():
                            pending.response = message
                            pending.ready.set()
                else:
                    raise ValueError("无法识别 JSON-RPC 消息")
        except Exception as exc:  # noqa: BLE001 -- 外部回调失败也必须唤醒所有等待者。
            self._fail(RpcTransportError("protocol_error", f"Runtime 读取失败：{exc}"))
        finally:
            self._fail(RpcTransportError("eof", "Runtime stdout 已关闭，未完成请求不得视为成功"))

    def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            while line := self._process.stderr.readline(8192):
                with self._lock:
                    self._stderr.append(line)
        except (OSError, UnicodeError, ValueError):
            pass

    def close(self) -> None:
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
            self._fail(RpcTransportError("closed", "Runtime 连接已关闭"))
            try:
                self._outbound.put_nowait(None)
            except queue.Full:
                pass  # 进程树终止会打断阻塞的 writer。
            process = self._process
            if process is None:
                return
            # 先回收整棵树，再关闭 reader 管道，避免后代持有句柄导致 close 永久阻塞。
            if self._tree:
                self._tree.kill()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for thread in (self._reader, self._stderr_reader, self._writer):
                if thread and thread is not threading.current_thread():
                    thread.join(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
