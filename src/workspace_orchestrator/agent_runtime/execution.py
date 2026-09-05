"""将交互 Runtime 适配为既有 Dispatcher 的同步执行端口。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .contracts import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    EventSink,
    RuntimeFailure,
    RuntimeOperationResult,
)
from .events import RuntimeEventStore
from .ports import AgentRuntimePort


@dataclass(slots=True)
class RuntimeExecutor:
    runtime_factory: Callable[[EventSink], AgentRuntimePort]
    event_store: RuntimeEventStore
    timeout_seconds: float = 7200
    allow_managed_hook_trust: bool = False

    def execute(
        self,
        workspace_path: Path,
        prompt: str,
        *,
        sandbox: str = "workspace-write",
        model: str | None = None,
        resume_session_id: str | None = None,
        bypass_hook_trust: bool = False,
    ) -> AgentRunResult:
        """仅在明确 Session 不存在时回退；超时和未知结果绝不重放输入。"""

        if (isinstance(self.timeout_seconds, bool)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("Runtime timeout_seconds 必须是有限正数")
        request = AgentRunRequest(
            run_id=str(uuid4()),
            workspace_path=workspace_path.resolve(),
            prompt=prompt,
            sandbox=sandbox,
            model=model,
            resume_session_id=resume_session_id,
            # 托管 Hook 检查来自兼容 Dispatcher；只向明确支持的 Adapter 传递授权。
            bypass_hook_trust=bypass_hook_trust and self.allow_managed_hook_trust,
            timeout_seconds=self.timeout_seconds,
        )

        def persist(event: AgentEvent) -> None:
            if event.run_id != request.run_id:
                raise ValueError("Runtime 事件不能写入其他 run_id")
            self.event_store.append(event)

        runtime: AgentRuntimePort | None = None
        started = time.monotonic()
        result = AgentRunResult(1, None, "", "Runtime 未返回结果", run_id=request.run_id)
        try:
            runtime = self.runtime_factory(persist)
            operation = runtime.resume(request) if resume_session_id else runtime.start(request)
            resumed = bool(resume_session_id)
            if (
                resume_session_id
                and not operation.ok
                and operation.error is not None
                and operation.error.code == "session_missing"
            ):
                operation = runtime.start(replace(request, resume_session_id=None))
                resumed = False
            if not operation.ok:
                result = self._failed(operation, request.run_id, resumed)
            elif operation.session is None or not operation.turn_id:
                result = self._failed(
                    RuntimeOperationResult(
                        "failed", error=RuntimeFailure("protocol_error", "Runtime 缺少 Session/Turn 引用")
                    ), request.run_id, resumed,
                )
            elif operation.session.run_id != request.run_id:
                result = self._failed(
                    RuntimeOperationResult(
                        "failed", error=RuntimeFailure("scope_mismatch", "Runtime 返回了其他 run_id")
                    ), request.run_id, resumed,
                )
            else:
                # 一旦外部 Session 已创建，即使 wait/清理失败也保留恢复身份。
                result = AgentRunResult(
                    1, operation.session.session_id, "", "Runtime 轮次尚未结束",
                    resumed=resumed, runtime_id=operation.session.runtime_id,
                    run_id=request.run_id,
                )
                remaining = self.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    runtime.interrupt(operation.session, operation.turn_id)
                    result = self._failed(
                        RuntimeOperationResult(
                            "timeout", session=operation.session,
                            error=RuntimeFailure("timeout", "Runtime 启动已超过运行时限"),
                        ), request.run_id, resumed,
                    )
                else:
                    result = runtime.wait(
                        operation.session, operation.turn_id, timeout_seconds=remaining
                    )
                    if (
                        result.session_id != operation.session.session_id
                        or result.run_id not in {None, request.run_id}
                    ):
                        result = self._failed(
                            RuntimeOperationResult(
                                "failed", error=RuntimeFailure("scope_mismatch", "Runtime 结果引用不匹配")
                            ), request.run_id, resumed,
                        )
                    else:
                        result = replace(result, run_id=request.run_id, resumed=resumed)
        except Exception as exc:  # noqa: BLE001 -- 外部可替换 Adapter 的故障边界。
            # 外部 Adapter 故障仍需返回结果，让既有 Dispatcher 进入可恢复阻塞态。
            result = replace(
                result, returncode=1, stderr=str(exc),
                error=RuntimeFailure("runtime_failure", str(exc)),
            )
        finally:
            try:
                if runtime is not None:
                    runtime.close()
            except Exception as exc:  # noqa: BLE001 -- 清理失败不得掩盖存活进程。
                result = replace(
                    result, returncode=1, stderr=f"Runtime 清理失败：{exc}",
                    error=RuntimeFailure("cleanup_failed", str(exc)),
                )
        return result

    @staticmethod
    def _failed(
        operation: RuntimeOperationResult, run_id: str, resumed: bool
    ) -> AgentRunResult:
        error = operation.error or RuntimeFailure(operation.status, "Runtime 操作失败")
        return AgentRunResult(
            returncode={"unavailable": 127, "unsupported": 64, "timeout": 124}.get(
                operation.status, 1
            ),
            session_id=operation.session.session_id if operation.session else None,
            stdout="",
            stderr=error.message,
            resumed=resumed,
            runtime_id=operation.session.runtime_id if operation.session else "unknown",
            run_id=run_id,
            summary=error.message,
            error=error,
        )
