"""本地需求生命周期的命令行界面。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters.agent import CodexAgentProvider
from .automation.requirement_attach import AutomationAmbiguity, discover_project_root
from .automation.runtime import AutomationRuntime
from .context import bootstrap_session, build_snapshot, checkpoint, handoff
from .models import WorkflowComplexity
from .workflow import route_workflow
from .workspace import WorkspaceError, WorkspaceStore, markdown_sections


def _display_state(value: str) -> str:
    labels = {
        "draft": "草稿",
        "ready": "就绪",
        "in_progress": "进行中",
        "in_review": "审查中",
        "done": "已完成",
        "tiny": "微型",
        "normal": "常规",
        "complex": "复杂",
        "research": "研究",
        "implementation": "实现",
    }
    label = labels.get(value)
    return f"{value}（{label}）" if label else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workspace",
        description="管理面向 AI 编码 Agent 的持久化需求工作区。",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    parser.add_argument(
        "--root", type=Path, default=None, help="项目根目录（默认：从当前路径自动发现）"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="创建需求工作区")
    new.add_argument("title")
    new.add_argument("--goal")
    new.add_argument("--acceptance", action="append", default=[])
    provider = new.add_mutually_exclusive_group()
    provider.add_argument("--task-provider", choices=("dashi",))
    provider.add_argument(
        "--no-task-provider",
        action="store_true",
        help="显式关闭项目默认 Task Provider",
    )
    new.add_argument("--task-project", help="外部任务板项目 ID")
    new.add_argument(
        "--complexity",
        choices=[item.value for item in WorkflowComplexity],
        default=None,
    )

    commands.add_parser("current", help="输出唯一的活动需求 ID")

    status = commands.add_parser("status", help="显示已持久化的需求状态")
    status.add_argument("requirement_id", nargs="?")

    resume = commands.add_parser("resume", help="恢复精简的上下文快照")
    resume.add_argument("requirement_id", nargs="?")
    resume.add_argument("--task", dest="task_ids", action="append", default=[])

    bootstrap = commands.add_parser("bootstrap", help="首次执行时自动接入需求")
    bootstrap.add_argument("requirement_id", nargs="?")
    bootstrap.add_argument("--task", dest="task_ids", action="append", default=[])
    bootstrap.add_argument("--request", dest="development_request")

    checkpoint_parser = commands.add_parser("checkpoint", help="持久化当前进度")
    checkpoint_parser.add_argument("requirement_id")
    checkpoint_parser.add_argument("--phase")
    checkpoint_parser.add_argument("--completed", action="append", default=[])
    checkpoint_parser.add_argument("--next-action")
    checkpoint_parser.add_argument("--verification")
    checkpoint_parser.add_argument("--task", dest="task_ids", action="append", default=[])

    handoff_parser = commands.add_parser("handoff", help="设置检查点并结束本次会话")
    handoff_parser.add_argument("requirement_id")
    handoff_parser.add_argument("--completed", action="append", default=[])
    handoff_parser.add_argument("--file", dest="files_changed", action="append", default=[])
    handoff_parser.add_argument("--current-state")
    handoff_parser.add_argument("--important-context")
    handoff_parser.add_argument("--next-action")
    handoff_parser.add_argument("--known-problems")
    handoff_parser.add_argument("--task", dest="task_ids", action="append", default=[])

    finalize = commands.add_parser(
        "finalize", help="自动验证、检查点、Task 审查、交接并结束 Session"
    )
    finalize.add_argument("requirement_id")
    finalize.add_argument("--completed", action="append", default=[])
    finalize.add_argument("--current-state")
    finalize.add_argument("--important-context")
    finalize.add_argument("--next-action")

    review = commands.add_parser("review", help="检查验收与验证门禁")
    review.add_argument("requirement_id")
    confirm = commands.add_parser("confirm", help="记录用户明确确认并完成已审查 Requirement")
    confirm.add_argument("requirement_id")
    confirm.add_argument(
        "--user-confirmed",
        action="store_true",
        help="确认这是用户明确批准的 in_review → done 转换",
    )
    changes = commands.add_parser(
        "request-changes", help="记录用户明确要求修改并重新打开已审查 Requirement"
    )
    changes.add_argument("requirement_id")
    changes.add_argument("--feedback", required=True, help="用户的明确修改反馈")
    changes.add_argument("--next-action", help="恢复开发后的下一步行动")
    return parser


def _status(store: WorkspaceStore, requirement_id: str) -> str:
    sync_messages = AutomationRuntime(store, CodexAgentProvider()).sync_reviews(requirement_id)
    data = store.load(requirement_id)
    meta = data["meta"]
    state = markdown_sections(data["state"])
    sessions = data["sessions"]
    result = (
        f"{meta['id']} {meta['title']}\n"
        f"状态：{_display_state(meta['status'])}\n"
        f"工作流：{_display_state(meta['workflow'])}\n"
        f"阶段：{_display_state(state.get('Phase', '未知'))}\n"
        f"下一步行动：{state.get('Next Action', '无')}\n"
        f"会话数：{len(sessions)}"
    )
    if sync_messages:
        result += "\n同步：" + "；".join(sync_messages)
    return result


def run(args: argparse.Namespace) -> str:
    project_root = args.root.resolve() if args.root else discover_project_root(Path.cwd())
    store = WorkspaceStore(
        project_root,
        execution_root=(args.root.resolve() if args.root else Path.cwd().resolve()),
    )
    agent_provider = CodexAgentProvider()
    if args.command == "new":
        if args.no_task_provider and args.task_project:
            raise WorkspaceError("--no-task-provider 不能与 --task-project 同时提供")
        complexity = (
            WorkflowComplexity(args.complexity)
            if args.complexity
            else route_workflow(" ".join(filter(None, (args.title, args.goal)))).complexity
        )
        provider_options: dict[str, object] = {}
        if args.no_task_provider:
            provider_options["task_provider"] = None
        elif args.task_provider:
            provider_options["task_provider"] = args.task_provider
        requirement_id = store.create(
            args.title,
            goal=args.goal,
            acceptance=args.acceptance,
            complexity=complexity,
            task_project_id=args.task_project,
            **provider_options,
        )
        return f"已创建 {requirement_id}"
    if args.command == "current":
        return store.current_id()
    if args.command == "status":
        return _status(store, args.requirement_id or store.current_id())
    if args.command == "resume":
        return build_snapshot(
            store,
            args.requirement_id or store.current_id(),
            agent_provider=agent_provider,
            task_ids=args.task_ids,
        ).rstrip()
    if args.command == "bootstrap":
        return bootstrap_session(
            store,
            args.requirement_id,
            agent_provider=agent_provider,
            task_ids=args.task_ids,
            development_request=args.development_request,
        ).rstrip()
    if args.command == "checkpoint":
        checkpoint(
            store,
            args.requirement_id,
            phase=args.phase,
            completed=args.completed,
            next_action=args.next_action,
            verification=args.verification,
            agent_provider=agent_provider,
            task_ids=args.task_ids,
        )
        return f"已为 {args.requirement_id.upper()} 设置检查点"
    if args.command == "handoff":
        handoff(
            store,
            args.requirement_id,
            completed=args.completed,
            files_changed=args.files_changed,
            current_state=args.current_state,
            important_context=args.important_context,
            next_action=args.next_action,
            known_problems=args.known_problems,
            agent_provider=agent_provider,
            task_ids=args.task_ids,
        )
        return f"已交接 {args.requirement_id.upper()}"
    if args.command == "finalize":
        result = AutomationRuntime(store, agent_provider).finalize(
            args.requirement_id,
            completed=args.completed,
            current_state=args.current_state,
            important_context=args.important_context,
            next_action=(
                args.next_action or "等待用户确认；Requirement 与 Task 均不得自动标记 done。"
            ),
        )
        if not result.passed:
            details = "\n".join(f"- {item}" for item in result.blockers)
            raise WorkspaceError(
                f"自动收尾受阻，已保存 checkpoint：{args.requirement_id.upper()}\n"
                f"{result.verification}\n阻塞项：\n{details}"
            )
        return (
            f"已完成自动收尾：{args.requirement_id.upper()}\n"
            f"Task 已进入 in_review：{', '.join(result.task_ids) or '无外部 Task'}\n"
            f"Requirement：{'in_review' if result.requirement_in_review else '等待审查门禁'}\n"
            f"Requirement Review Task：{result.review_task_id or '未配置'}\n"
            f"{result.verification}"
        )
    if args.command == "review":
        result = AutomationRuntime(store, agent_provider).review(args.requirement_id)
        if result.passed:
            return (
                f"意图审查：{result.intent_status}\n"
                f"可进入审查：{args.requirement_id.upper()} 已处于 in_review 状态"
            )
        details = "\n".join(f"- {blocker}" for blocker in result.blockers)
        raise WorkspaceError(
            f"意图审查：{result.intent_status}\n审查受阻：{args.requirement_id.upper()}\n{details}"
        )
    if args.command == "confirm":
        AutomationRuntime(store, agent_provider).confirm(
            args.requirement_id,
            user_confirmed=args.user_confirmed,
        )
        return f"用户已确认：{args.requirement_id.upper()} 已进入 done；外部 Task 未自动完成"
    if args.command == "request-changes":
        AutomationRuntime(store, agent_provider).request_changes(
            args.requirement_id,
            feedback=args.feedback,
            next_action=args.next_action,
        )
        return (
            f"用户已要求修改：{args.requirement_id.upper()} 已恢复 in_progress；"
            "in_review 开发 Task 已重新打开，done Task 保持不变"
        )
    raise AssertionError(f"未处理的命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        output = run(build_parser().parse_args(argv))
    except AutomationAmbiguity as exc:
        print(f"状态：ambiguity\n错误：{exc}", file=sys.stderr)
        return 2
    except WorkspaceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
