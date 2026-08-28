"""本地需求生命周期的命令行界面。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters.agent import CodexAgentProvider
from .context import bootstrap_session, build_snapshot, checkpoint, handoff
from .models import WorkflowComplexity
from .review import review_requirement
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
        "--root", type=Path, default=Path.cwd(), help="项目根目录（默认：当前目录）"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="创建需求工作区")
    new.add_argument("title")
    new.add_argument("--goal")
    new.add_argument("--acceptance", action="append", default=[])
    new.add_argument("--task-provider", choices=("dashi",))
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

    review = commands.add_parser("review", help="检查验收与验证门禁")
    review.add_argument("requirement_id")
    return parser


def _status(store: WorkspaceStore, requirement_id: str) -> str:
    data = store.load(requirement_id)
    meta = data["meta"]
    state = markdown_sections(data["state"])
    sessions = data["sessions"]
    return (
        f"{meta['id']} {meta['title']}\n"
        f"状态：{_display_state(meta['status'])}\n"
        f"工作流：{_display_state(meta['workflow'])}\n"
        f"阶段：{_display_state(state.get('Phase', '未知'))}\n"
        f"下一步行动：{state.get('Next Action', '无')}\n"
        f"会话数：{len(sessions)}"
    )


def run(args: argparse.Namespace) -> str:
    store = WorkspaceStore(args.root.resolve())
    agent_provider = CodexAgentProvider()
    if args.command == "new":
        if bool(args.task_provider) != bool(args.task_project):
            raise WorkspaceError("--task-provider 和 --task-project 必须同时提供")
        complexity = (
            WorkflowComplexity(args.complexity)
            if args.complexity
            else route_workflow(" ".join(filter(None, (args.title, args.goal)))).complexity
        )
        requirement_id = store.create(
            args.title,
            goal=args.goal,
            acceptance=args.acceptance,
            complexity=complexity,
            task_provider=args.task_provider,
            task_project_id=args.task_project,
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
    if args.command == "review":
        result = review_requirement(store, args.requirement_id)
        if result.passed:
            return (
                f"意图审查：{result.intent_status}\n"
                f"可进入审查：{args.requirement_id.upper()} 已处于 in_review 状态"
            )
        details = "\n".join(f"- {blocker}" for blocker in result.blockers)
        raise WorkspaceError(
            f"意图审查：{result.intent_status}\n"
            f"审查受阻：{args.requirement_id.upper()}\n{details}"
        )
    raise AssertionError(f"未处理的命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        output = run(build_parser().parse_args(argv))
    except WorkspaceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
