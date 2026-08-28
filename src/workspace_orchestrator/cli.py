"""Command-line interface for the local requirement lifecycle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .context import build_snapshot, checkpoint, handoff
from .models import WorkflowComplexity
from .review import review_requirement
from .workflow import route_workflow
from .workspace import WorkspaceError, WorkspaceStore, markdown_sections


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workspace",
        description="Manage persistent requirement workspaces for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="Project root (default: current directory)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="Create a requirement workspace")
    new.add_argument("title")
    new.add_argument("--goal")
    new.add_argument("--acceptance", action="append", default=[])
    new.add_argument("--task-provider", choices=("dashi",))
    new.add_argument("--task-project", help="External Taskboard project ID")
    new.add_argument(
        "--complexity",
        choices=[item.value for item in WorkflowComplexity],
        default=None,
    )

    commands.add_parser("current", help="Print the only active requirement ID")

    status = commands.add_parser("status", help="Show persisted requirement status")
    status.add_argument("requirement_id", nargs="?")

    resume = commands.add_parser("resume", help="Restore a concise context snapshot")
    resume.add_argument("requirement_id", nargs="?")

    checkpoint_parser = commands.add_parser("checkpoint", help="Persist current progress")
    checkpoint_parser.add_argument("requirement_id")
    checkpoint_parser.add_argument("--phase")
    checkpoint_parser.add_argument("--completed", action="append", default=[])
    checkpoint_parser.add_argument("--next-action")
    checkpoint_parser.add_argument("--verification")

    handoff_parser = commands.add_parser("handoff", help="Checkpoint and end this session")
    handoff_parser.add_argument("requirement_id")
    handoff_parser.add_argument("--completed", action="append", default=[])
    handoff_parser.add_argument("--file", dest="files_changed", action="append", default=[])
    handoff_parser.add_argument("--current-state")
    handoff_parser.add_argument("--important-context")
    handoff_parser.add_argument("--next-action")
    handoff_parser.add_argument("--known-problems")

    review = commands.add_parser("review", help="Check acceptance and verification gates")
    review.add_argument("requirement_id")
    return parser


def _status(store: WorkspaceStore, requirement_id: str) -> str:
    data = store.load(requirement_id)
    meta = data["meta"]
    state = markdown_sections(data["state"])
    sessions = data["sessions"]
    return (
        f"{meta['id']} {meta['title']}\n"
        f"Status: {meta['status']}\n"
        f"Workflow: {meta['workflow']}\n"
        f"Phase: {state.get('Phase', 'unknown')}\n"
        f"Next action: {state.get('Next Action', 'None')}\n"
        f"Sessions: {len(sessions)}"
    )


def run(args: argparse.Namespace) -> str:
    store = WorkspaceStore(args.root.resolve())
    if args.command == "new":
        if bool(args.task_provider) != bool(args.task_project):
            raise WorkspaceError("--task-provider and --task-project must be supplied together")
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
        return f"Created {requirement_id}"
    if args.command == "current":
        return store.current_id()
    if args.command == "status":
        return _status(store, args.requirement_id or store.current_id())
    if args.command == "resume":
        return build_snapshot(store, args.requirement_id or store.current_id()).rstrip()
    if args.command == "checkpoint":
        checkpoint(
            store,
            args.requirement_id,
            phase=args.phase,
            completed=args.completed,
            next_action=args.next_action,
            verification=args.verification,
        )
        return f"Checkpointed {args.requirement_id.upper()}"
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
        )
        return f"Handed off {args.requirement_id.upper()}"
    if args.command == "review":
        result = review_requirement(store, args.requirement_id)
        if result.passed:
            return f"Review ready: {args.requirement_id.upper()} is in_review"
        details = "\n".join(f"- {blocker}" for blocker in result.blockers)
        raise WorkspaceError(f"Review blocked: {args.requirement_id.upper()}\n{details}")
    raise AssertionError(f"Unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        output = run(build_parser().parse_args(argv))
    except WorkspaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
