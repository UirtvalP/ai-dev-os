"""Phase 6 main-only 部署门禁的最小可用实现。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from .integration.contracts import MergeReceipt
from .workspace import WorkspaceError, _file_lock


class DeploymentError(WorkspaceError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeploymentPolicy:
    environment: str
    provider_version: str
    require_remote_main: bool = True


@dataclass(frozen=True, slots=True)
class DeploymentAuthorization:
    authorization_id: str
    requirement_id: str
    environment: str
    commit_sha: str
    merge_receipt_id: str
    verification_receipt_id: str


@dataclass(frozen=True, slots=True)
class DeploymentReceipt:
    receipt_id: str
    requirement_id: str
    environment: str
    commit_sha: str
    provider_version: str
    status: Literal["succeeded", "failed"]
    started_at: str
    completed_at: str
    rollback: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CompletionToken:
    token_id: str
    requirement_id: str
    commit_sha: str
    deployment_receipt_id: str | None
    issued_at: str


class DeploymentProvider(Protocol):
    def deploy(self, *, environment: str, commit_sha: str) -> tuple[bool, str, str]:
        """返回 success、detail、rollback。"""


class MainStateProvider(Protocol):
    def state(self) -> tuple[str, str | None, bool, str]:
        """返回 branch、remote main SHA、clean、local HEAD。"""


class DeploymentService:
    def __init__(self, root: Path, policy: DeploymentPolicy, main: MainStateProvider,
                 provider: DeploymentProvider) -> None:
        self.root, self.policy, self.main, self.provider = root, policy, main, provider

    def deploy(self, authorization: DeploymentAuthorization, merge: MergeReceipt,
               *, post_merge_verification_receipt_id: str) -> DeploymentReceipt:
        self._validate(authorization, merge, post_merge_verification_receipt_id)
        key = f"{authorization.requirement_id}-{self.policy.environment}-{authorization.commit_sha}-{self.policy.provider_version}"
        path = self.root / f"{key}.json"
        with _file_lock(path.with_suffix(".lock")):
            if path.exists():
                return DeploymentReceipt(**json.loads(path.read_text(encoding="utf-8")))
            # 真正副作用之前重查，关闭授权检查与执行之间的漂移窗口。
            self._validate(authorization, merge, post_merge_verification_receipt_id)
            started = datetime.now(UTC).isoformat()
            try:
                success, detail, rollback = self.provider.deploy(
                    environment=self.policy.environment, commit_sha=authorization.commit_sha)
            except (OSError, RuntimeError) as exc:
                success, detail, rollback = False, f"Provider unavailable: {exc}", ""
            receipt = DeploymentReceipt(
                f"deployment-{uuid4().hex}", authorization.requirement_id,
                self.policy.environment, authorization.commit_sha, self.policy.provider_version,
                "succeeded" if success else "failed", started, datetime.now(UTC).isoformat(),
                rollback, detail,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            temporary.write_text(json.dumps(asdict(receipt), ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, path)
            return receipt

    def complete(self, requirement_id: str, commit_sha: str,
                 *, deployment_required: bool, receipt: DeploymentReceipt | None = None) -> CompletionToken:
        if deployment_required and (receipt is None or receipt.status != "succeeded"
                                    or receipt.commit_sha != commit_sha
                                    or receipt.requirement_id != requirement_id):
            raise DeploymentError("deployment_required", "缺少当前提交的成功部署收据")
        return CompletionToken(f"completion-{uuid4().hex}", requirement_id, commit_sha,
                               receipt.receipt_id if receipt else None,
                               datetime.now(UTC).isoformat())

    def _validate(self, authorization: DeploymentAuthorization, merge: MergeReceipt,
                  verification_receipt_id: str) -> None:
        branch, remote_sha, clean, head = self.main.state()
        if branch != "main" or not clean or head != authorization.commit_sha:
            raise DeploymentError("not_protected_main", "部署目标必须是干净本地 main 的当前提交")
        if self.policy.require_remote_main and remote_sha != head:
            raise DeploymentError("remote_main_drift", "本地 main 与远端 main 不一致")
        if authorization.environment != self.policy.environment:
            raise DeploymentError("authorization_mismatch", "部署环境未获授权")
        if (merge.status != "merged" or merge.requirement_id != authorization.requirement_id
                or merge.merged_sha != head or merge.receipt_id != authorization.merge_receipt_id):
            raise DeploymentError("invalid_merge_receipt", "Merge Receipt 缺失、失败或陈旧")
        if (not merge.post_merge_verification_receipt_id
                or merge.post_merge_verification_receipt_id != verification_receipt_id
                or authorization.verification_receipt_id != verification_receipt_id):
            raise DeploymentError("invalid_verification_receipt", "post-merge Verification Receipt 缺失或陈旧")
