"""Git 集成的版本化凭据；可反序列化不等于取得签发权限。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from ..orchestration.contracts import _Contract, _sha, _strings, _text
from ..workspace import WorkspaceError


class IntegrationError(WorkspaceError):
    """拒绝缺失、过期或有歧义的集成，保留现场供恢复。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _time_window(issued_at: str, expires_at: str) -> None:
    try:
        start, end = datetime.fromisoformat(issued_at), datetime.fromisoformat(expires_at)
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("时间必须带时区且有序")
    except (TypeError, ValueError) as exc:
        raise IntegrationError("invalid_authorization", "凭据时间窗口不合法") from exc


@dataclass(frozen=True, slots=True)
class RequirementReviewApproval(_Contract):
    requirement_id: str
    snapshot_fingerprint: str
    candidate_sha: str
    candidate_tree: str
    review_fingerprint: str
    issued_at: str
    expires_at: str
    authority_id: str

    def validate(self) -> None:
        _Contract.validate(self)
        _text(self.requirement_id, "requirement_id")
        _text(self.authority_id, "authority_id")
        for name in ("snapshot_fingerprint", "review_fingerprint"):
            _sha(getattr(self, name), name, digest=True)
        for name in ("candidate_sha", "candidate_tree"):
            _sha(getattr(self, name), name)
        _time_window(self.issued_at, self.expires_at)


class RequirementReviewPort(Protocol):
    """由 composition 注入的可信 V1 审查；禁止来自请求 JSON 的 approval。"""

    def review(
        self, requirement_id: str, snapshot_fingerprint: str,
        candidate_sha: str, candidate_tree: str,
    ) -> RequirementReviewApproval: ...

    def revalidate(self, approval: RequirementReviewApproval) -> None: ...

    def guard(self, approval: RequirementReviewApproval) -> AbstractContextManager[None]:
        """复用现有 Workspace/Task 状态锁，只覆盖最后检查和 ref 副作用短段。"""
        ...


@dataclass(frozen=True, slots=True)
class IntegrationAuthorization(_Contract):
    authorization_id: str
    requirement_id: str
    request_id: str
    request_fingerprint: str
    snapshot_fingerprint: str
    task_ids: tuple[str, ...]
    expected_main_sha: str
    candidate_sha: str
    candidate_tree: str
    review_fingerprint: str
    verification_receipt_id: str
    issued_at: str
    expires_at: str

    def validate(self) -> None:
        _Contract.validate(self)
        for name in ("authorization_id", "requirement_id", "request_id", "verification_receipt_id"):
            _text(getattr(self, name), name)
        _strings(self.task_ids, "task_ids", nonempty=True)
        for name in ("request_fingerprint", "snapshot_fingerprint", "review_fingerprint"):
            _sha(getattr(self, name), name, digest=True)
        for name in ("expected_main_sha", "candidate_sha", "candidate_tree"):
            _sha(getattr(self, name), name)
        _time_window(self.issued_at, self.expires_at)

    @classmethod
    def _decode(cls, values: dict[str, Any]) -> dict[str, Any]:
        if isinstance(values.get("task_ids"), list):
            values["task_ids"] = tuple(values["task_ids"])
        return values


@dataclass(frozen=True, slots=True)
class MergeReceipt(_Contract):
    """仅证明 Git 集成结果；没有 CompletionToken，也不授予部署权限。"""

    receipt_id: str
    requirement_id: str
    request_id: str
    status: Literal["merged", "recovery_required"]
    expected_main_sha: str
    merged_sha: str
    merged_tree: str
    integration_ref: str
    authorization_id: str
    integration_verification_receipt_id: str
    post_merge_verification_receipt_id: str | None
    completed_at: str
    reason: str = ""

    def validate(self) -> None:
        _Contract.validate(self)
        for name in ("receipt_id", "requirement_id", "request_id", "integration_ref",
                     "authorization_id", "integration_verification_receipt_id", "completed_at"):
            _text(getattr(self, name), name)
        for name in ("expected_main_sha", "merged_sha", "merged_tree"):
            _sha(getattr(self, name), name)
        if self.status not in ("merged", "recovery_required"):
            raise IntegrationError("invalid_receipt", "未知合并结果")
        if self.status == "merged" and not self.post_merge_verification_receipt_id:
            raise IntegrationError("invalid_receipt", "成功合并必须具有 post-merge 证据")
        if self.post_merge_verification_receipt_id is not None:
            _text(self.post_merge_verification_receipt_id, "post_merge_verification_receipt_id")
        if not isinstance(self.reason, str):
            raise IntegrationError("invalid_receipt", "reason 必须是字符串")
