"""一次触发后连续执行确定性 Workspace 生命周期的运行时。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from workspace_orchestrator.adapters.agent import AgentProviderError
from workspace_orchestrator.adapters.base import AgentProvider, TaskProvider, TaskProviderError
from workspace_orchestrator.project_config import load_project_config
from workspace_orchestrator.review import (
    ReviewResult,
    confirm_requirement_done,
    request_requirement_changes,
    review_requirement,
    sync_requirement_review_outcome,
)
from workspace_orchestrator.review_packet import (
    build_review_packet,
    parse_review_packet_marker,
    render_review_packet,
    validate_review_packet,
)
from workspace_orchestrator.workflow import route_workflow
from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore

from .git_sync import collect_git_context, sync_task_git_context
from .requirement_attach import parse_new_requirement_request, select_requirement
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
    complete_tasks,
    configured_task_provider,
    ensure_requirement_board_task,
    ensure_requirement_review_task,
    ensure_requirement_space_task,
    ensure_requirement_task_parent,
    move_tasks_to_review,
    select_tasks,
)


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    passed: bool
    verification: str
    task_ids: tuple[str, ...]
    requirement_in_review: bool = False
    requirement_completed: bool = False
    blockers: tuple[str, ...] = ()
    review_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class AutoFinishResult:
    completed: bool
    reason: str
    requirement_id: str | None = None
    task_ids: tuple[str, ...] = ()


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
        return configured_task_provider(
            self.store.load(requirement_id)["meta"], self.store.project_root
        )

    def _current_packet_fingerprint(
        self, requirement_id: str, provider: TaskProvider | None
    ) -> str | None:
        if provider is None:
            return None
        tasks, task_error = list_tasks_safely(provider, requirement_id)
        if task_error:
            raise WorkspaceError(f"无法验证 Review Packet 当前事实：{task_error}")
        data = self.store.load(requirement_id)
        git = collect_git_context(
            self.store.project_root,
            dict(data["meta"].get("git") or {}),
            execution_root=self.store.working_root,
        )
        return build_review_packet(self.store, requirement_id, tasks=tasks, git=git).fingerprint

    def sync_reviews(self, requirement_id: str | None = None) -> tuple[str, ...]:
        """同步所有待审查结果和离线待补偿状态，不创建或完成 Review 卡。"""

        requirement_ids = (
            (requirement_id.upper(),)
            if requirement_id
            else self.store.requirement_ids(statuses={"in_review", "in_progress"})
        )
        messages: list[str] = []
        for current_id in requirement_ids:
            provider = self._provider(current_id)
            current_fingerprint = None
            if provider is not None:
                try:
                    current_fingerprint = self._current_packet_fingerprint(current_id, provider)
                except WorkspaceError:
                    current_fingerprint = None
            message = sync_requirement_review_outcome(
                self.store,
                current_id,
                provider,
                current_packet_fingerprint=current_fingerprint,
            )
            if message:
                messages.append(message)
        return tuple(messages)

    def sync_taskboard_visibility(self, skip_requirement_id: str | None = None) -> tuple[str, ...]:
        """幂等补偿需求空间与活动工作卡；Provider 离线不破坏本地状态。"""

        messages: list[str] = []
        skipped = skip_requirement_id.upper() if skip_requirement_id else None
        for current_id in self.store.requirement_ids(
            statuses={"draft", "ready", "in_progress", "blocked", "in_review", "done"}
        ):
            if current_id == skipped:
                continue
            try:
                ensure_requirement_space_task(self.store, current_id, self._provider(current_id))
                if self.store.load(current_id)["meta"].get("status") == "done":
                    ensure_requirement_task_parent(
                        self.store, current_id, self._provider(current_id)
                    )
                    continue
                ensure_requirement_board_task(self.store, current_id, self._provider(current_id))
                ensure_requirement_task_parent(self.store, current_id, self._provider(current_id))
            except WorkspaceError as exc:
                messages.append(f"{current_id}：{exc}")
        return tuple(messages)

    def bootstrap(
        self,
        requirement_id: str | None = None,
        *,
        task_ids: Sequence[str] = (),
        development_request: str | None = None,
        creation_key: str | None = None,
    ) -> str:
        """完成 Session→Requirement→Task→dashi→Git→Snapshot 全链路。"""

        session_id = require_session_id(self.agent_provider)
        pending_requirement_id = self.store.requirement_id_for_session(
            session_id, results={"pending_auto_finish"}
        )
        new_request = parse_new_requirement_request(development_request)
        if pending_requirement_id and (
            new_request is not None
            or (requirement_id is not None and requirement_id.upper() != pending_requirement_id)
        ):
            raise WorkspaceError(
                f"当前 Thread 正在等待 {pending_requirement_id} 的提交推送后自动归档；"
                "请先完成该收尾，再创建或切换 Requirement"
            )
        if pending_requirement_id:
            requirement_id = pending_requirement_id
        if new_request is not None:
            requirement_id = self.store.create(
                new_request.title,
                goal=new_request.goal,
                complexity=route_workflow(new_request.goal).complexity,
                manual_test_required=new_request.manual_test_required,
                creation_key=creation_key,
            )
        sync_messages = self.sync_reviews(requirement_id)
        visibility_target = requirement_id or self.store.attached_requirement_id(session_id)
        if visibility_target is None:
            try:
                visibility_target = self.store.current_id()
            except WorkspaceError:
                visibility_target = None
        visibility_messages = self.sync_taskboard_visibility(visibility_target)
        if pending_requirement_id:
            snapshot = self.snapshot(pending_requirement_id, attach=False)
            messages = (*sync_messages, *visibility_messages)
            return "\n".join((*messages, "", snapshot)).lstrip() if messages else snapshot
        if requirement_id and self.store.load(requirement_id)["meta"].get("status") == "done":
            provider = self._provider(requirement_id)
            tasks, task_error = list_tasks_safely(provider, requirement_id)
            snapshot = collect_snapshot(
                self.store,
                requirement_id,
                tasks=tasks,
                task_error=task_error,
            )
            prefix = "\n".join(sync_messages)
            return f"{prefix}\n\n{snapshot}" if prefix else snapshot
        selected_id, attached_id = select_requirement(self.store, session_id, requirement_id)
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
        git = collect_git_context(
            self.store.project_root,
            dict(meta.get("git") or {}),
            execution_root=self.store.working_root,
        )
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
            head_commit=str(git["head"]) if git.get("head") else None,
            branch=str(git["branch"]) if git.get("branch") else None,
            worktree=str(git["worktree"]) if git.get("worktree") else None,
        )
        try:
            ensure_requirement_board_task(self.store, selected_id, provider)
        except WorkspaceError as exc:
            visibility_messages = (
                *visibility_messages,
                f"{selected_id}：{exc}",
            )
        snapshot = collect_snapshot(
            self.store,
            selected_id,
            tasks=selection.tasks,
            task_error=selection.task_error,
            git=git,
        )
        messages = (*sync_messages, *visibility_messages)
        return "\n".join((*messages, "", snapshot)).lstrip() if messages else snapshot

    def snapshot(
        self,
        requirement_id: str,
        *,
        task_ids: Sequence[str] = (),
        attach: bool = True,
    ) -> str:
        """兼容 resume：自动读取结构化事实，并可幂等注册当前 Session。"""

        self.sync_reviews(requirement_id)
        provider = self._provider(requirement_id)
        tasks, task_error = list_tasks_safely(provider, requirement_id)
        data = self.store.load(requirement_id)
        git = collect_git_context(
            self.store.project_root,
            dict(data["meta"].get("git") or {}),
            execution_root=self.store.working_root,
        )
        if attach and self.store.load(requirement_id)["meta"].get("status") != "done":
            session_id = self.agent_provider.current_session_id()
            if session_id:
                selected = tuple(dict.fromkeys(task_ids))
                if not selected and not task_error:
                    active = [task for task in tasks if task.status == "in_progress"]
                    if len(active) > 1:
                        from .requirement_attach import AutomationAmbiguity

                        raise AutomationAmbiguity(
                            f"ambiguity：需求 {requirement_id} 存在多个 in_progress Task，"
                            "请使用 --task 明确指定：" + ", ".join(task.id for task in active)
                        )
                    selected = tuple(task.id for task in active)
                attach_session(
                    self.store,
                    requirement_id,
                    session_id=session_id,
                    agent_name=self.agent_provider.name,
                    task_provider=provider,
                    task_ids=selected,
                    head_commit=str(git["head"]) if git.get("head") else None,
                )
        return collect_snapshot(
            self.store, requirement_id, tasks=tasks, task_error=task_error, git=git
        )

    def auto_finish_pushed_thread(self) -> AutoFinishResult:
        """在 Stop Hook 中按项目配置完成已推送 Task 并归档当前 Thread。"""

        config = load_project_config(self.store.project_root)
        if config is None:
            return AutoFinishResult(False, "项目未通过 ai-dev-os init 接入")
        if not config.auto_finish_pushed_thread:
            return AutoFinishResult(False, "项目已关闭已推送 Thread 自动收尾")
        session_id = require_session_id(self.agent_provider)
        requirement_id = self.store.requirement_id_for_session(
            session_id, results={"pending_auto_finish"}
        ) or self.store.attached_requirement_id(session_id)
        if not requirement_id:
            return AutoFinishResult(False, "当前 Thread 未绑定 Requirement")
        data = self.store.load(requirement_id)
        session = next(
            (
                item
                for item in data["sessions"]
                if item.get("id") == session_id
                and item.get("result") in {"in_progress", "pending_auto_finish"}
            ),
            None,
        )
        if session is None:
            return AutoFinishResult(False, "当前 Thread 没有待归档记录", requirement_id)
        task_ids = tuple(dict.fromkeys(session.get("task_ids", ())))
        if not task_ids:
            return AutoFinishResult(False, "当前 Thread 未绑定开发 Task", requirement_id)
        started_head = session.get("started_head")
        git = collect_git_context(
            self.store.project_root,
            dict(data["meta"].get("git") or {}),
            execution_root=Path(str(session.get("worktree") or self.store.working_root)),
        )
        current_head = git.get("head")
        if not started_head or not current_head or current_head == started_head:
            return AutoFinishResult(
                False, "当前 Thread 启动后没有产生新提交", requirement_id, task_ids
            )
        bound_branch = session.get("branch") or (data["meta"].get("git") or {}).get("branch")
        if bound_branch and git.get("branch") != bound_branch:
            return AutoFinishResult(
                False, "当前分支与 Requirement 绑定分支不一致", requirement_id, task_ids
            )
        if not git.get("clean"):
            return AutoFinishResult(False, "工作树仍有未提交变更", requirement_id, task_ids)
        if not git.get("upstream"):
            return AutoFinishResult(False, "当前分支没有上游", requirement_id, task_ids)
        if not git.get("pushed"):
            return AutoFinishResult(False, "当前提交尚未与上游完全同步", requirement_id, task_ids)
        provider = self._provider(requirement_id)
        complete_tasks(provider, task_ids)
        try:
            self.agent_provider.archive_session(session_id)
        except AgentProviderError as exc:
            raise WorkspaceError(f"Thread 自动归档失败：{exc}") from exc
        end_session(
            self.store,
            requirement_id,
            session_id,
            result="completed",
            task_provider=provider,
            allowed_results=("in_progress", "pending_auto_finish"),
        )
        return AutoFinishResult(True, "关联 Task 已完成且 Thread 已归档", requirement_id, task_ids)

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
            selected = tuple(task_ids) or session_task_ids(self.store, requirement_id, session_id)
            attach_session(
                self.store,
                requirement_id,
                session_id=session_id,
                agent_name=self.agent_provider.name,
                task_provider=self._provider(requirement_id),
                task_ids=selected,
            )

    def request_changes(
        self,
        requirement_id: str,
        *,
        feedback: str,
        next_action: str | None = None,
    ) -> None:
        request_requirement_changes(
            self.store,
            requirement_id,
            feedback=feedback,
            next_action=next_action,
            task_provider=self._provider(requirement_id),
        )

    def confirm(self, requirement_id: str, *, user_confirmed: bool) -> None:
        """显式确认；配置 Provider 时仍必须通过当前 Review Packet 门禁。"""

        provider = self._provider(requirement_id)
        fingerprint = self._current_packet_fingerprint(requirement_id, provider)
        confirm_requirement_done(
            self.store,
            requirement_id,
            user_confirmed=user_confirmed,
            task_provider=provider,
            current_packet_fingerprint=fingerprint,
        )

    def review(self, requirement_id: str) -> ReviewResult:
        """在组合层注入 Provider 后执行 Core review gate。"""

        before = self.store.load(requirement_id)["meta"].get("status")
        messages = self.sync_reviews(requirement_id)
        after = self.store.load(requirement_id)["meta"].get("status")
        if after == "done" or (before == "in_review" and after != before):
            raise WorkspaceError("；".join(messages) or f"Requirement 当前为 {after}")
        provider = self._provider(requirement_id)
        result = review_requirement(
            self.store, requirement_id, task_provider=provider, transition=False
        )
        if not result.passed:
            return result
        blockers, _ = self._publish_review_packet(requirement_id, provider)
        if blockers:
            from workspace_orchestrator.review import ReviewResult

            return ReviewResult(False, blockers, result.intent_status)
        return result

    def _publish_review_packet(
        self, requirement_id: str, provider: TaskProvider | None
    ) -> tuple[tuple[str, ...], object | None]:
        """发布完整 Packet；成功后才把本地与 Review 卡推进到 in_review。"""

        if provider is None:
            self.store.touch_meta(requirement_id, status="in_review")
            return (), None
        tasks, task_error = list_tasks_safely(provider, requirement_id)
        if task_error:
            return (f"Review Packet 发布失败：{task_error}",), None
        data = self.store.load(requirement_id)
        git = collect_git_context(
            self.store.project_root,
            dict(data["meta"].get("git") or {}),
            execution_root=self.store.working_root,
        )
        packet = build_review_packet(self.store, requirement_id, tasks=tasks, git=git)
        blockers = validate_review_packet(packet, git_error=git.get("error"))
        if blockers:
            return blockers, None
        try:
            with self.store.provider_locked(requirement_id):
                fresh_meta = self.store.load(requirement_id)["meta"]
                old_fingerprint = fresh_meta.get("review_packet_fingerprint")
                old_revision = int(fresh_meta.get("review_packet_revision") or 0)
                revision = (
                    old_revision if old_fingerprint == packet.fingerprint else old_revision + 1
                )
                content = render_review_packet(packet, revision)
                review_task = ensure_requirement_review_task(
                    provider,
                    requirement_id,
                    str(fresh_meta["title"]),
                    expected_task_id=(
                        str(fresh_meta["requirement_review_task_id"])
                        if fresh_meta.get("requirement_review_task_id")
                        else None
                    ),
                )
                if review_task is None:
                    return ("Review Packet 发布失败：未创建专用 Review 卡",), None
                review_task = provider.publish_review(review_task.id, content)
                marker = parse_review_packet_marker(review_task.description)
                if marker != (requirement_id.upper(), revision, packet.fingerprint):
                    return ("Review Packet 发布失败：Provider 未返回当前 revision 正文",), None
                if review_task.status != "in_review":
                    review_task = provider.update_status(review_task.id, "in_review")
                comments = tuple(provider.list_comments(review_task.id))
                # 发布后重建事实；期间若证据变化，不能提交 review-ready。
                refreshed_tasks, refreshed_error = list_tasks_safely(provider, requirement_id)
                refreshed_git = collect_git_context(
                    self.store.project_root,
                    dict(self.store.load(requirement_id)["meta"].get("git") or {}),
                    execution_root=self.store.working_root,
                )
                refreshed = build_review_packet(
                    self.store, requirement_id, tasks=refreshed_tasks, git=refreshed_git
                )
                if refreshed_error or refreshed.fingerprint != packet.fingerprint:
                    return ("Review Packet 发布期间审查证据发生变化，请重试",), None
                self.store.touch_meta(
                    requirement_id,
                    status="in_review",
                    requirement_review_task_id=review_task.id,
                    review_comment_count=len(comments),
                    review_packet_revision=revision,
                    review_packet_fingerprint=packet.fingerprint,
                    review_packet_published_revision=revision,
                    review_packet_published_fingerprint=packet.fingerprint,
                    review_packet_stale=False,
                )
                return (), review_task
        except (WorkspaceError, TaskProviderError) as exc:
            # TaskProviderError 及测试 Provider 的确定性发布失败均转为可重试 blocker。
            self.store.touch_meta(requirement_id, status="in_progress")
            return (f"Review Packet 发布失败：{exc}",), None

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
        git = collect_git_context(
            self.store.project_root,
            dict(data["meta"].get("git") or {}),
            execution_root=self.store.working_root,
        )
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

    def _finish_or_defer_session(
        self,
        requirement_id: str,
        session_id: str,
        task_ids: Sequence[str],
        provider: TaskProvider | None,
    ) -> None:
        """finalize 后保留可由 Stop Hook 收敛的待推送归档记录。"""

        config = load_project_config(self.store.project_root)
        pending = bool(config and config.auto_finish_pushed_thread and task_ids and provider)
        end_session(
            self.store,
            requirement_id,
            session_id,
            result="pending_auto_finish" if pending else "completed",
            task_provider=provider,
        )

    def finalize(
        self,
        requirement_id: str,
        *,
        completed: Sequence[str] = (),
        current_state: str | None = None,
        important_context: str | None = None,
        next_action: str = "提交并推送后由 Stop Hook 自动归档 Thread。",
    ) -> FinalizeResult:
        """串行同一 Requirement 的终态转换，避免并发 finalize 回滚完成状态。"""

        with self.store.finalize_locked(requirement_id):
            return self._finalize_once(
                requirement_id,
                completed=completed,
                current_state=current_state,
                important_context=important_context,
                next_action=next_action,
            )

    def _finalize_once(
        self,
        requirement_id: str,
        *,
        completed: Sequence[str] = (),
        current_state: str | None = None,
        important_context: str | None = None,
        next_action: str = "提交并推送后由 Stop Hook 自动归档 Thread。",
    ) -> FinalizeResult:
        """一次触发执行验证、checkpoint、Task review、handoff 与 detach。"""

        self.sync_reviews(requirement_id)
        initial_meta = self.store.load(requirement_id)["meta"]
        initial_status = initial_meta.get("status")
        manual_test_required = bool(initial_meta.get("manual_test_required"))
        final_next_action = (
            "等待明确要求的人工测试或验收。" if manual_test_required else next_action
        )
        if initial_status in {"in_review", "done"}:
            return FinalizeResult(
                False,
                "状态：FAIL",
                (),
                blockers=(
                    f"Requirement 当前为 {initial_status}，不能重复 finalize；请先完成用户审查流程",
                ),
            )
        session_id = require_session_id(self.agent_provider)
        task_ids = session_task_ids(self.store, requirement_id, session_id)
        try:
            results = run_known_verifications(self.store.working_root)
        except WorkspaceError as exc:
            summary = f"状态：FAIL\n- FAIL：{exc}"
            self.checkpoint(
                requirement_id,
                phase="verification",
                next_action=f"修复自动验证配置：{exc}",
                verification=summary,
                task_ids=task_ids,
            )
            return FinalizeResult(False, summary, task_ids, blockers=(str(exc),))
        summary = verification_summary(results)
        passed = bool(results) and all(item.passed for item in results)
        persist_verification_results(self.store, requirement_id, results)
        self.checkpoint(
            requirement_id,
            phase="verification" if not passed else "review",
            completed=completed,
            next_action=final_next_action if passed else "修复失败的自动验证后重新 finalize。",
            verification=summary,
            task_ids=task_ids,
        )
        if not passed:
            verification_blockers = tuple(
                item.output or f"验证命令失败：{' '.join(item.command)}"
                for item in results
                if not item.passed
            ) or ("未配置已知验证命令",)
            return FinalizeResult(False, summary, task_ids, blockers=verification_blockers)
        provider = self._provider(requirement_id)
        review_candidates = list(task_ids)
        if provider is not None:
            related_tasks, task_error = list_tasks_safely(provider, requirement_id)
            if task_error is None:
                review_candidates.extend(
                    task.id
                    for task in related_tasks
                    if "requirement-review" not in task.labels
                    and "requirement-space" not in task.labels
                    and task.status not in {"in_review", "done"}
                )
        try:
            move_tasks_to_review(provider, tuple(dict.fromkeys(review_candidates)))
        except WorkspaceError as exc:
            self.checkpoint(
                requirement_id,
                phase="verification",
                next_action=f"修复 Task 状态同步：{exc}",
                verification=summary,
                task_ids=task_ids,
            )
            return FinalizeResult(False, summary, task_ids, blockers=(str(exc),))
        review = review_requirement(
            self.store, requirement_id, task_provider=provider, transition=False
        )
        if not review.passed:
            blocker_text = "；".join(review.blockers)
            self.checkpoint(
                requirement_id,
                phase="verification",
                next_action=f"修复 Requirement review blockers：{blocker_text}",
                verification=summary,
                task_ids=task_ids,
            )
            persist_handoff(
                self.store,
                requirement_id,
                session_id=session_id,
                completed=completed,
                current_state="自动验证通过，但 Requirement review 仍受阻。",
                important_context=important_context,
                next_action=f"修复审查阻塞项：{blocker_text}",
                known_problems=blocker_text,
            )
            return FinalizeResult(
                False,
                summary,
                task_ids,
                blockers=review.blockers,
            )
        final_task_ids = tuple(dict.fromkeys(review_candidates))
        if final_task_ids != task_ids:
            attach_session(
                self.store,
                requirement_id,
                session_id=session_id,
                agent_name=self.agent_provider.name,
                task_provider=provider,
                task_ids=final_task_ids,
            )
        # 先固化 handoff/Git 事实，再完成或生成人工 Review Packet。
        data = self.store.load(requirement_id)
        git = collect_git_context(
            self.store.project_root,
            dict(data["meta"].get("git") or {}),
            execution_root=self.store.working_root,
        )
        changed = tuple(
            path
            for path in git.get("changed_files", ())
            if path != ".workspace" and not path.startswith(".workspace/")
        )
        persist_handoff(
            self.store,
            requirement_id,
            session_id=session_id,
            completed=completed,
            files_changed=changed,
            current_state=current_state or "实现、代码审阅与已知验证均已完成。",
            important_context=important_context,
            next_action=final_next_action,
        )
        if manual_test_required:
            packet_blockers, review_task = self._publish_review_packet(requirement_id, provider)
            if packet_blockers:
                self.store.touch_meta(requirement_id, status="in_progress")
                return FinalizeResult(False, summary, final_task_ids, blockers=packet_blockers)
            self._finish_or_defer_session(requirement_id, session_id, final_task_ids, provider)
            return FinalizeResult(
                True,
                summary,
                final_task_ids,
                requirement_in_review=True,
                review_task_id=getattr(review_task, "id", None),
            )
        try:
            if provider is not None:
                complete_tasks(provider, final_task_ids)
        except WorkspaceError as exc:
            self.store.touch_meta(requirement_id, status="in_progress")
            return FinalizeResult(False, summary, final_task_ids, blockers=(str(exc),))
        self.store.touch_meta(
            requirement_id,
            status="done",
            completion_mode="auto_after_verification",
        )
        self._finish_or_defer_session(requirement_id, session_id, final_task_ids, provider)
        return FinalizeResult(
            True,
            summary,
            final_task_ids,
            requirement_completed=True,
        )
