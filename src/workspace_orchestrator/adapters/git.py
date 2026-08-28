"""与工作区核心隔离的本地 Git 适配器。"""

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
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise GitError(f"Git 不可用：{exc}") from exc
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "未知的 Git 错误"
            raise GitError(message)
        return result.stdout.rstrip("\r\n")

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

    def changed_files(self) -> tuple[str, ...]:
        """返回工作树报告的已跟踪与未跟踪路径。"""

        output = self._run("status", "--porcelain=v1", "-z")
        entries = output.split("\0") if output else []
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry:
                index += 1
                continue
            status = entry[:2]
            path = entry[3:]
            if "R" in status or "C" in status:
                # 使用 -z 时，第一个路径是目标，后一个路径是来源。
                index += 1
            if path:
                paths.append(path.replace("\\", "/"))
            index += 1
        return tuple(dict.fromkeys(paths))
