"""CLI entry point; lifecycle commands are implemented milestone by milestone."""

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workspace",
        description="Manage persistent requirement workspaces for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    return parser


def main() -> None:
    build_parser().parse_args()
