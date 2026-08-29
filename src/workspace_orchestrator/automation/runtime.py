"""一次触发后连续执行确定性 Workspace 生命周期的运行时。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from workspace_orchestrator.adapters.base import AgentProvider, TaskProvider
from workspace_orchestrator.review import review_requirement
from workspace_orchestrator.workspace import WorkspaceStore

from .git_sync import collect_git_context, sync_task_git_context
from .requirement_attach import select_requirement
from .session_runtime import attach_session, end_session, require_session_id, session_task_ids
from .state_sync import (
    collect_snapshot,
    list_tasks_safely,
    persist_checkpoint,
    persist_handoff,
    persist_verification_results,
    run_known_verifications,
    verification_summary,
)
from .task_attach import (
    configured_task_provider,
    ensure_project_task_services,
    move_tasks_to_review,
    select_tasks,
)


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    passed: bool
    verification: str
    task_ids: tuple[str, ...]
    requirement_in_review: bool = False


class AutomationRuntime:
    """无 LLM、无对话历史依赖的确定性执行器。"""

    def __init__(
        self,
        store: WorkspaceStore,
        agent_provider: AgentProvider,
        task_provider: TaskProvider | None = None,
    ) -> None:
        self.store = store
        self.agent_provider = agent_provider
        self._task_provider = task_provider

    def _provider(self, requirement_id: str) -> TaskProvider | None:
        if self._task_provider is not None:
            return self._task_provider
        return configured_task_provider(self.store.load(requirement_id)["meta"])

    def bootstrap(
        self,
        requirement_id: str | None = None,
        *,
        task_ids: Sequence[str] = (),
        development_request: str | None = None,
    ) -> str:
        """完成 Session→Requirement→Task→dashi→Git→Snapshot 全链路。"""

        session_id = require_session_id(self.agent_provider)
        ensure_project_task_services(self.store)
        selected_id, attached_id = select_requirement(
            self.store, session_id, requirement_id
        )
        provider = self._provider(selected_id)
        bound_ids = (
            session_task_ids(self.store, selected_id, session_id)
            if attached_id == selected_id
            else ()
        )
        selection = select_tasks(
            selected_id,
            provider,
            explicit_task_ids=task_ids,
            bound_task_ids=bound_ids,
            development_request=development_request,
        )
        # 目标选择和校验全部成功后才允许切断旧 Requirement。
        if attached_id and attached_id != selected_id:
            end_session(
                self.store,
                attached_id,
                session_id,
                task_provider=self._provider(attached_id),
            )
        data = self.store.load(selected_id)
        meta = data["meta"]
        if meta["status"] in {"draft", "ready"}:
            meta = self.store.touch_meta(selected_id, status="in_progress")
        git = collect_git_context(self.store.project_root, dict(meta.get("git") or {}))
        stored_git = dict(meta.get("git") or {})
        stored_git.update({key: git[key] for key in ("branch", "worktree") if git.get(key)})
        if stored_git != meta.get("git"):
            self.store.touch_meta(selected_id, git=stored_git)
        sync_task_git_context(provider, selection.task_ids, selection.tasks, git)
        attach_session(
            self.store,
            selected_id,
            session_id=session_id,
            agent_name=self.agent_provider.name,
            task_provider=provider,
            task_ids=selection.task_ids,
        )
        return collect_snapshot(
            self.store,
            selected_id,
            tasks=selection.tasks,
            git=git,
        )

    def snapshot(
        self,
        requirement_id: str,
        *,
        task_ids: Sequence[str] = (),
        attach: bool = True,
    ) -> str:
        """兼容 resume：自动读取结构化事实，并可幂等注册当前 Session。"""

        provider = self._provider(requirement_id)
        tasks, task_error = list_tasks_safely(provider, requirement_id)
        if attach:
            session_id = self.agent_provider.current_session_id()
            if session_id:
                selected = tuple(dict.fromkeys(task_ids))
                if not selected and not task_error:
                    active = [task for task in tasks if task.status == "in_progress"]
                    if len(active) > 1:
                        from .requirement_attach import AutomationAmbiguity

                        raise AutomationAmbiguity(
                            f"ambiguity：需求 {requirement_id} 存在多个 in_progress Task，"
                            "请使用 --task 明确指定："
                            + ", ".join(task.id for task in active)
                        )
                    selected = tuple(task.id for task in active)
                attach_session(
                    self.store,
                    requirement_id,
                    session_id=session_id,
                    agent_name=self.agent_provider.name,
                    task_provider=provider,
                    task_ids=selected,
                )
        return collect_snapshot(
            self.store, requirement_id, tasks=tasks, task_error=task_error
        )

    def checkpoint(
        self,
        requirement_id: str,
        *,
        phase: str | None = None,
        completed: Sequence[str] = (),
        next_action: str | None = None,
        verification: str | None = None,
        task_ids: Sequence[str] = (),
    ) -> None:
        persist_checkpoint(
            self.store,
            requirement_id,
            phase=phase,
            completed=completed,
            next_action=next_action,
            verification=verification,
        )
        session_id = self.agent_provider.current_session_id()
        if session_id:
            selected = tuple(task_ids) or session_task_ids(
                self.store, requirement_id, session_id
            )
            attach_session(
                self.store,
                requirement_id,
                session_id=session_id,
                agent_name=self.agent_provider.name,
                task_provider=self._provider(requirement_id),
                task_ids=selected,
            )

    def handoff(
        self,
        requirement_id: str,
        *,
        completed: Sequence[str] = (),
        files_changed: Sequence[str] = (),
        current_state: str | None = None,
        important_context: str | None = None,
        next_action: str | None = None,
        known_problems: str | None = None,
        task_ids: Sequence[str] = (),
    ) -> None:
        self.checkpoint(
            requirement_id,
            completed=completed,
            next_action=next_action,
            task_ids=task_ids,
        )
        session_id = self.agent_provider.current_session_id() or "未知"
        data = self.store.load(requirement_id)
        git = collect_git_context(self.store.project_root, dict(data["meta"].get("git") or {}))
        changed = list(
            dict.fromkeys(
                path
                for path in [*git.get("changed_files", ()), *files_changed]
                if path != ".workspace" and not path.startswith(".workspace/")
            )
        )
        persist_handoff(
            self.store,
            requirement_id,
            session_id=session_id,
            completed=completed,
            files_changed=changed,
            current_state=current_state,
            important_context=important_context,
            next_action=next_action,
            known_problems=known_problems,
        )
        if session_id != "未知":
            end_session(
                self.store,
                requirement_id,
                session_id,
                result="completed",
                task_provider=self._provider(requirement_id),
            )

    def finalize(
        self,
        requirement_id: str,
        *,
        completed: Sequence[str] = (),
        current_state: str | None = None,
        important_context: str | None = None,
        next_action: str = "等待用户确认；Requirement 与 Task 均不得自动标记 done。",
    ) -> FinalizeResult:
        """一次触发执行验证、checkpoint、Task review、handoff 与 detach。"""

        session_id = require_session_id(self.agent_provider)
        task_ids = session_task_ids(self.store, requirement_id, session_id)
        results = run_known_verifications(self.store.project_root)
        summary = verification_summary(results)
        passed = bool(results) and all(item.passed for item in results)
        persist_verification_results(self.store, requirement_id, results)
        self.checkpoint(
            requirement_id,
            phase="verification" if not passed else "review",
            completed=completed,
            next_action=next_action if passed else "修复失败的自动验证后重新 finalize。",
            verification=summary,
            task_ids=task_ids,
        )
        if not passed:
            return FinalizeResult(False, summary, task_ids)
        move_tasks_to_review(self._provider(requirement_id), task_ids)
        review = review_requirement(
            self.store, requirement_id, task_provider=self._provider(requirement_id)
        )
        self.handoff(
            requirement_id,
            completed=completed,
            current_state=current_state or "实现与已知验证均已完成，等待用户确认。",
            important_context=important_context,
            next_action=next_action,
            task_ids=task_ids,
        )
        return FinalizeResult(True, summary, task_ids, requirement_in_review=review.passed)
