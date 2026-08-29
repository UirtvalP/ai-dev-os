"""AI Dev OS 产品级命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .project_init import InitResult, initialize_project
from .workspace import WorkspaceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-dev-os",
        description="将现有项目接入 AI Dev OS。",
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
    return parser


def _format_result(result: InitResult) -> str:
    lines = [f"AI Dev OS 已接入：{result.root}"]
    labels = (
        ("已创建", result.created),
        ("已更新", result.updated),
        ("已保留", result.preserved),
    )
    lines.extend(f"{label}：{', '.join(paths)}" for label, paths in labels if paths)
    lines.append("下一步：使用 workspace new 创建首个 Requirement。")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> str:
    if args.command == "init":
        return _format_result(initialize_project(args.path))
    raise AssertionError(f"未处理的命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        output = run(build_parser().parse_args(argv))
    except (OSError, UnicodeError, WorkspaceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
