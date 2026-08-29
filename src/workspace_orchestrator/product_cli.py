"""AI Dev OS 产品级命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .adapters.agent import CodexExecProvider
from .adapters.package import ToolInstallerError, ToolUpgradeResult, UvToolInstaller
from .automation.dispatcher import (
    AutoDispatcher,
    dispatcher_status,
    serve_dispatcher,
    start_dispatcher,
    stop_dispatcher,
)
from .automation.requirement_attach import discover_project_root
from .hook_runtime import main as hook_main
from .project_init import InitResult, initialize_project, migrate_project
from .workspace import WorkspaceError, WorkspaceStore

DEFAULT_UPGRADE_SOURCE = "git+https://github.com/UirtvalP/ai-dev-os.git"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-dev-os",
        description="管理全局 AI Dev OS CLI 与项目接入。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="将现有项目接入 AI Dev OS")
    init_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="项目目录（默认：当前目录）",
    )
    upgrade_parser = commands.add_parser("upgrade", help="更新全局 AI Dev OS CLI")
    upgrade_parser.add_argument(
        "--source",
        default=DEFAULT_UPGRADE_SOURCE,
        help="安装来源（默认：AI Dev OS 官方 Git 仓库）",
    )
    migrate_parser = commands.add_parser("migrate", help="迁移已接入项目的持久文件格式")
    migrate_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="已接入的项目目录（默认：当前目录）",
    )
    commands.add_parser("hook", help=argparse.SUPPRESS)
    dispatcher = commands.add_parser(
        "dispatcher", help="管理 dashi → Codex 自动执行 Dispatcher"
    )
    dispatcher_commands = dispatcher.add_subparsers(dest="dispatcher_command", required=True)
    for name, help_text in (
        ("start", "启动后台 Dispatcher"),
        ("stop", "停止后台 Dispatcher"),
        ("status", "查看 Dispatcher 状态"),
        ("run-once", "同步检查一次并处理至多一个 Task"),
        ("serve", argparse.SUPPRESS),
    ):
        command = dispatcher_commands.add_parser(name, help=help_text)
        command.add_argument(
            "--root",
            type=Path,
            default=Path.cwd(),
            help="项目或关联 worktree 目录（默认：当前目录）",
        )
    return parser


def _format_result(result: InitResult, *, action: str) -> str:
    lines = [f"AI Dev OS {action}：{result.root}"]
    labels = (
        ("已创建", result.created),
        ("已更新", result.updated),
        ("已保留", result.preserved),
    )
    lines.extend(f"{label}：{', '.join(paths)}" for label, paths in labels if paths)
    if action == "已接入":
        lines.append("下一步：使用 workspace new 创建首个 Requirement。")
    return "\n".join(lines)


def _format_upgrade_result(result: ToolUpgradeResult) -> str:
    state = "更新已安排" if result.scheduled else "已更新"
    lines = [f"AI Dev OS 全局 CLI {state}。", f"来源：{result.source}"]
    if result.details:
        lines.append(result.details)
    if result.result_path:
        lines.append(f"结果日志：{result.result_path}")
    lines.append("更新完成后，后续 ai-dev-os、workspace 与项目 Hook 调用将使用新版本能力。")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> str:
    if args.command == "init":
        result = initialize_project(args.path)
        status = start_dispatcher(
            WorkspaceStore(result.root, execution_root=result.root)
        )
        suffix = (
            "\nDispatcher：已启动，dashi Task 移到 in_progress 后会自动执行。"
            if status.get("running") or status.get("status") == "starting"
            else f"\nDispatcher：{status.get('status', '未启动')}。"
        )
        return _format_result(result, action="已接入") + suffix
    if args.command == "upgrade":
        return _format_upgrade_result(UvToolInstaller().upgrade(args.source))
    if args.command == "migrate":
        return _format_result(migrate_project(args.path), action="项目格式已迁移")
    if args.command == "dispatcher":
        execution_root = args.root.expanduser().resolve()
        project_root = discover_project_root(execution_root)
        store = WorkspaceStore(project_root, execution_root=execution_root)
        if args.dispatcher_command == "start":
            return json.dumps(start_dispatcher(store, explicit=True), ensure_ascii=False, indent=2)
        if args.dispatcher_command == "stop":
            return json.dumps(stop_dispatcher(store), ensure_ascii=False, indent=2)
        if args.dispatcher_command == "status":
            return json.dumps(dispatcher_status(store), ensure_ascii=False, indent=2)
        if args.dispatcher_command == "run-once":
            return AutoDispatcher(store, CodexExecProvider()).run_once()
    raise AssertionError(f"未处理的命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    effective = sys.argv[1:] if argv is None else argv
    if effective == ["hook"]:
        return hook_main()
    try:
        args = build_parser().parse_args(effective)
        if args.command == "dispatcher" and args.dispatcher_command == "serve":
            execution_root = args.root.expanduser().resolve()
            project_root = discover_project_root(execution_root)
            return serve_dispatcher(
                WorkspaceStore(project_root, execution_root=execution_root)
            )
        output = run(args)
    except (OSError, ToolInstallerError, UnicodeError, WorkspaceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
