"""Local Git adapter, isolated from the workspace core."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(slots=True)
class LocalGitProvider:
    project_root: Path

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.project_root,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise GitError(f"Git is unavailable: {exc}") from exc
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
            raise GitError(message)
        return result.stdout.strip()

    def status(self) -> dict[str, object]:
        porcelain = self._run("status", "--porcelain")
        return {
            "branch": self.current_branch(),
            "worktree": str(self.project_root.resolve()),
            "clean": not bool(porcelain),
            "changes": tuple(line for line in porcelain.splitlines() if line),
        }

    def current_branch(self) -> str | None:
        return self._run("branch", "--show-current") or None

    def create_branch(self, name: str) -> None:
        self._run("branch", name)

    def create_worktree(self, path: str, branch: str) -> None:
        self._run("worktree", "add", path, branch)

    def recent_commits(self, limit: int = 5) -> tuple[str, ...]:
        output = self._run("log", f"-{limit}", "--pretty=format:%h %s")
        return tuple(output.splitlines())

    def diff(self) -> str:
        return self._run("diff", "--no-ext-diff")
