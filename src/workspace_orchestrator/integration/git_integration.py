"""原生 Git 合并与 CAS 的薄适配器，不执行候选代码或仓库脚本。"""

from __future__ import annotations

from pathlib import Path

from .contracts import IntegrationError
from .git_workspace import GitWorkspaceDirtyError, GitWorkspaceError, TrustedGit


class GitIntegrationAdapter:
    def __init__(
        self, git: TrustedGit, *, main_branch: str = "main", remote: str = "origin",
        preserved_roots: tuple[Path, ...] = (),
    ) -> None:
        if main_branch != "main":
            raise IntegrationError("protected_branch", "Phase 3 只支持受保护 main")
        if not remote or remote.startswith("-") or any(char.isspace() for char in remote):
            raise IntegrationError("invalid_remote", "必须使用已配置 remote 名称")
        self.git, self.main_ref, self.remote = git, "refs/heads/main", remote
        self.preserved_roots = preserved_roots

    def main_checkout(self) -> Path | None:
        found: list[Path] = []
        for entry in self.git.run("worktree", "list", "--porcelain").split("\n\n"):
            lines = entry.splitlines()
            if f"branch {self.main_ref}" in lines:
                worktrees = [line[9:] for line in lines if line.startswith("worktree ")]
                if len(worktrees) != 1:
                    raise IntegrationError("invalid_worktree", "main worktree 信息不完整")
                found.append(Path(worktrees[0]).resolve())
        if len(found) > 1:
            raise IntegrationError("shared_main", "main 被多个工作树占用，拒绝集成")
        return found[0] if found else None

    def assert_main(self, expected: str, *, remote: bool = True) -> Path | None:
        if self.git.resolve(self.main_ref) != expected:
            raise IntegrationError("main_drift", "main HEAD 与 expected main SHA 不一致")
        checkout = self.main_checkout()
        if checkout is not None:
            self.git.assert_clean(checkout, revision=expected, preserved_roots=self.preserved_roots,
                                  native_checkout=True)
        if remote:
            self.assert_remote(expected)
        return checkout

    def assert_remote(self, expected: str) -> None:
        """实时读取远端，不把陈旧 origin/main 当成当前远端事实。"""
        remote_sha = self.git.remote_tip(self.remote, self.main_ref)
        try:
            behind = not self.git.is_ancestor(remote_sha, expected)
        except Exception as exc:
            raise IntegrationError("remote_ahead", "远端 main 不在当前已知 main 历史中") from exc
        if behind:
            raise IntegrationError("remote_ahead", "本地 main 落后或分叉，必须先显式同步")

    def build_candidate(
        self, expected: str, candidates: tuple[str, ...], *, identity: str, created_at: str,
    ) -> tuple[str, str]:
        current, changed = expected, False
        for candidate in candidates:
            if self.git.is_ancestor(candidate, current):
                continue
            try:
                tree = self.git.run("merge-tree", "--write-tree", "--no-messages", current, candidate)
            except Exception as exc:
                raise IntegrationError("merge_conflict", f"候选不能无冲突集成：{exc}") from exc
            tree = tree.splitlines()[0]
            current = self.git.run(
                "commit-tree", tree, "-p", current, "-p", candidate,
                input=f"AI Dev OS integration {identity}\n".encode(),
                extra_env={
                    "GIT_AUTHOR_NAME": "AI Dev OS", "GIT_AUTHOR_EMAIL": "integration@localhost",
                    "GIT_COMMITTER_NAME": "AI Dev OS", "GIT_COMMITTER_EMAIL": "integration@localhost",
                    "GIT_AUTHOR_DATE": created_at, "GIT_COMMITTER_DATE": created_at,
                },
            )
            changed = True
        if not changed:
            raise IntegrationError("already_integrated", "候选全部已在 main 中，不重复生成合并")
        return current, self.git.run("rev-parse", "--verify", current + "^{tree}")

    def ensure_diagnostic(
        self, candidate: str, ref: str, worktree: Path, *, initialize: bool = False,
    ) -> None:
        """仅占用自己持久意图中的分支/路径，永不覆盖已有不同内容。"""
        entries = self.git.tree_entries(candidate)
        refs = self.git.run("for-each-ref", "--format=%(refname) %(objectname)", ref).splitlines()
        known = [line.split()[1] for line in refs if line.split()[0] == ref]
        if known:
            if known != [candidate]:
                raise IntegrationError("integration_ref_drift", "诊断分支已有不同结果")
        else:
            self.git.run("update-ref", ref, candidate, "0" * len(candidate))
        if not worktree.exists():
            self.git.run("worktree", "add", "--no-checkout", "--detach", str(worktree), candidate)
        if self.git.resolve("HEAD", cwd=worktree) != candidate:
            raise IntegrationError("integration_worktree_drift", "诊断工作树 HEAD 已变化")
        if initialize:
            # 与 Task Workspace 共用 raw blob 物化，不让 gitattributes/eol/filter 改写候选。
            self.git._materialize(entries, worktree)
            self.git.run("read-tree", candidate, cwd=worktree)
        self.git.assert_clean(worktree, revision=candidate)

    def publish(self, expected: str, candidate: str) -> None:
        # 原生事务把 main CAS 与发布身份一起提交。即使用户后来回退 main，也不能
        # 把已经发生过的发布误判为 ref 前崩溃并重复执行。
        transaction = (f"start\nupdate {self.main_ref} {candidate} {expected}\n"
                       f"create {self.publication_ref(candidate)} {candidate}\nprepare\ncommit\n")
        self.git.run("update-ref", "--stdin", "--create-reflog", "-m",
                     "AI Dev OS authorized integration", input=transaction.encode("ascii"))

    @staticmethod
    def publication_ref(candidate: str) -> str:
        return "refs/ai-dev-os/integrated/" + candidate

    def publication_observed(self, candidate: str) -> bool:
        ref = self.publication_ref(candidate)
        entries = self.git.run("for-each-ref", "--format=%(refname) %(objectname)", ref).splitlines()
        matching = [item for item in entries if item.split()[0] == ref]
        if matching and matching != [ref + " " + candidate]:
            raise IntegrationError("publication_marker_drift", "原子发布标记已被替换，不能推断恢复状态")
        return bool(matching)

    def reconcile_checkout(self, expected: str, candidate: str, checkout: str | None) -> None:
        """ref 已更新后，只接受磁盘完整旧版或完整新版；不清除任何未知修改。"""
        actual = self.main_checkout()
        if (str(actual) if actual is not None else None) != checkout:
            raise IntegrationError("main_worktree_drift", "发布期间 main checkout 注册发生变化")
        if actual is None:
            return
        try:
            self.git.assert_clean(actual, revision=candidate, preserved_roots=self.preserved_roots,
                                  native_checkout=True)
            return
        except GitWorkspaceError as candidate_error:
            # 新版索引上的真实用户编辑会先在这里以“未提交修改”出现。此时旧树
            # 必然因索引属于 candidate 而不匹配，不能让次级索引错误覆盖用户错误。
            # 反之，旧树上的用户编辑会在下方检查中出现，仍应优先保留其诊断。
            try:
                self.git.assert_clean(actual, revision=expected,
                                      preserved_roots=self.preserved_roots, native_checkout=True)
            except GitWorkspaceError as expected_error:
                if isinstance(expected_error, GitWorkspaceDirtyError):
                    raise
                raise candidate_error
        # 内容已按 Git 内建 EOL 规则证明等同旧树。core.autocrlf 下普通 refresh
        # 仍可能把 CRLF 工作文件报告为 needs update；复用 Git 自带 renormalize
        # 刷新全部 tracked 项，再次证明索引和工作文件仍等同旧树。可信调用已禁用
        # 外部 filter/hook，若期间出现用户修改，第二次检查或 read-tree 会失败关闭。
        self.git.run("add", "--renormalize", "--update", "--", cwd=actual)
        self.git.assert_clean(actual, revision=expected, preserved_roots=self.preserved_roots,
                              native_checkout=True)
        self.git.run("read-tree", "-m", "-u", expected, candidate, cwd=actual)
        self.git.assert_clean(actual, revision=candidate, preserved_roots=self.preserved_roots,
                              native_checkout=True)
