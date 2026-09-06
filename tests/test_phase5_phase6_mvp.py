from pathlib import Path

import pytest

from workspace_orchestrator.dashboard import CommandQueue
from workspace_orchestrator.deployment import (
    DeploymentAuthorization,
    DeploymentError,
    DeploymentPolicy,
    DeploymentService,
)
from workspace_orchestrator.integration.contracts import MergeReceipt


def test_command_queue_is_persistent_and_idempotent(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path / "commands.json")
    first = queue.enqueue("REQ-020", "session-1", "继续", command_id="cmd-1")
    assert queue.enqueue("REQ-020", "session-1", "继续", command_id="cmd-1") == first
    assert CommandQueue(tmp_path / "commands.json").pending("session-1") == (first,)
    assert queue.update("cmd-1", "completed", "ok").status == "completed"


class Main:
    def __init__(self, sha: str, branch: str = "main") -> None:
        self.sha, self.branch = sha, branch

    def state(self) -> tuple[str, str | None, bool, str]:
        return self.branch, self.sha, True, self.sha


class Provider:
    calls = 0

    def deploy(self, *, environment: str, commit_sha: str) -> tuple[bool, str, str]:
        self.calls += 1
        return True, "ok", "rollback"


def test_deployment_is_main_only_and_idempotent(tmp_path: Path) -> None:
    sha, tree = "a" * 40, "b" * 40
    merge = MergeReceipt("merge-1", "REQ-020", "request-1", "merged", "c" * 40,
                         sha, tree, "refs/heads/integration", "integration-auth",
                         "pre", "post", "2026-09-07T00:00:00+00:00")
    auth = DeploymentAuthorization("deploy-auth", "REQ-020", "prod", sha, "merge-1", "post")
    provider = Provider()
    service = DeploymentService(tmp_path, DeploymentPolicy("prod", "1"), Main(sha), provider)
    first = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    second = service.deploy(auth, merge, post_merge_verification_receipt_id="post")
    assert first == second and provider.calls == 1
    assert service.complete("REQ-020", sha, deployment_required=True,
                            receipt=first).deployment_receipt_id == first.receipt_id
    assert service.complete("REQ-020", sha, deployment_required=False).deployment_receipt_id is None

    blocked = DeploymentService(tmp_path / "blocked", DeploymentPolicy("prod", "1"),
                                Main(sha, "feature"), provider)
    with pytest.raises(DeploymentError, match="main"):
        blocked.deploy(auth, merge, post_merge_verification_receipt_id="post")
