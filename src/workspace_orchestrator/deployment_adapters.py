"""Phase 6 的本地 Git 事实与无副作用部署适配器。"""

from __future__ import annotations

from pathlib import Path

from .integration.git_workspace import TrustedGit


class LocalGitMainStateProvider:
    """每次调用都实时解析本地 HEAD、工作树与远端受保护 main。"""

    def __init__(
        self, root: Path, *, remote: str = "origin", main_ref: str = "refs/heads/main",
    ) -> None:
        self.git = TrustedGit(root)
        self.remote = remote
        self.main_ref = main_ref

    def state(self) -> tuple[str | None, str | None, bool, str]:
        branch = self.git.run("branch", "--show-current") or None
        head = self.git.run("rev-parse", "--verify", "HEAD")
        clean = not bool(self.git.run("status", "--porcelain=v1", "--untracked-files=all"))
        remote_sha = self.git.remote_tip(self.remote, self.main_ref)
        return branch, remote_sha, clean, head
