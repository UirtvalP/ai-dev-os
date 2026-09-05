"""绑定 exact Git SHA 的版本化阶段门禁记录。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Protocol, Self

from .adapters.base import TaskProvider, TaskProviderError
from .adapters.git import GitError, LocalGitProvider
from .models import Task
from .workspace import WorkspaceError, WorkspaceStore, now_iso

SCHEMA_VERSION = 1
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
_RECEIPT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class PhaseGateError(WorkspaceError):
    """阶段门禁记录缺失、损坏或不再匹配当前事实。"""


class GitRevisionReader(Protocol):
    """阶段门禁所需的最小只读 Git 能力。"""

    def head_sha(self) -> str: ...

    def is_clean(self) -> bool: ...

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool: ...

    def read_file_at(self, revision: str, relative_path: str) -> str: ...

    def list_files_at(self, revision: str, prefix: str) -> tuple[str, ...]: ...


def content_fingerprint(value: object) -> str:
    """对 JSON 可表达的计划或验收定义生成确定性 SHA-256 指纹。"""

    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PhaseGateError(f"无法为非 JSON 内容生成指纹：{exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_fingerprint(value: str) -> str:
    """对已提交源文件内容生成 SHA-256。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _schema_version(payload: Mapping[str, object]) -> int:
    value = payload.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PhaseGateError("schema_version 必须是正整数")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PhaseGateError(f"{key} 必须是非空字符串")
    return value


def _optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PhaseGateError(f"{key} 必须是整数或 null")
    return value


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PhaseGateError(f"{key} 必须是非空字符串或 null")
    return value


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PhaseGateError(f"{key} 必须是字符串数组")
    if any(not item.strip() for item in value):
        raise PhaseGateError(f"{key} 不能包含空字符串")
    return tuple(value)


def _object_mapping(value: object, key: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(item, str) for item in value):
        raise PhaseGateError(f"{key} 必须是 JSON 对象")
    return value


def _extra_json(payload: Mapping[str, object], known_fields: frozenset[str]) -> str:
    """把未知的非权威 metadata 冻结成规范 JSON，以便 load/dump 保真。"""

    return json.dumps(
        {key: value for key, value in payload.items() if key not in known_fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _with_extra(extra_json: str, fields: dict[str, object]) -> dict[str, object]:
    try:
        extra = _object_mapping(json.loads(extra_json), "未知 JSON 字段")
    except (json.JSONDecodeError, TypeError) as exc:
        raise PhaseGateError(f"未知 JSON 字段不是有效 JSON：{exc}") from exc
    result = dict(extra)
    result.update(fields)
    return result


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    """一个稳定 Acceptance ID 在某次 Phase Gate 中的不可变结果。"""

    acceptance_id: str
    status: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION
    _extra_fields_json: str = "{}"

    def to_dict(self) -> dict[str, object]:
        return _with_extra(
            self._extra_fields_json,
            {
                "schema_version": self.schema_version,
                "acceptance_id": self.acceptance_id,
                "status": self.status,
                "summary": self.summary,
                "evidence_refs": list(self.evidence_refs),
            },
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        # 未知字段被有意忽略，使旧 Reader 能读取新增可选字段的未来 schema。
        return cls(
            acceptance_id=_required_string(payload, "acceptance_id"),
            status=_required_string(payload, "status"),
            summary=_required_string(payload, "summary"),
            evidence_refs=_string_tuple(payload, "evidence_refs"),
            schema_version=_schema_version(payload),
            _extra_fields_json=_extra_json(
                payload,
                frozenset(
                    {
                        "schema_version",
                        "acceptance_id",
                        "status",
                        "summary",
                        "evidence_refs",
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewAttestation:
    """未参与实现的 Reviewer 对阶段候选结果给出的不可变证明。"""

    reviewer_session_id: str
    implementation_session_ids: tuple[str, ...]
    implementation_run_ids: tuple[str, ...]
    verdict: str
    resolved_findings: tuple[str, ...]
    reviewed_at: str
    schema_version: int = SCHEMA_VERSION
    _extra_fields_json: str = "{}"

    def to_dict(self) -> dict[str, object]:
        return _with_extra(
            self._extra_fields_json,
            {
                "schema_version": self.schema_version,
                "reviewer_session_id": self.reviewer_session_id,
                "implementation_session_ids": list(self.implementation_session_ids),
                "implementation_run_ids": list(self.implementation_run_ids),
                "verdict": self.verdict,
                "resolved_findings": list(self.resolved_findings),
                "reviewed_at": self.reviewed_at,
            },
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            reviewer_session_id=_required_string(payload, "reviewer_session_id"),
            implementation_session_ids=_string_tuple(
                payload, "implementation_session_ids"
            ),
            implementation_run_ids=_string_tuple(payload, "implementation_run_ids"),
            verdict=_required_string(payload, "verdict"),
            resolved_findings=_string_tuple(payload, "resolved_findings"),
            reviewed_at=_required_string(payload, "reviewed_at"),
            schema_version=_schema_version(payload),
            _extra_fields_json=_extra_json(
                payload,
                frozenset(
                    {
                        "schema_version",
                        "reviewer_session_id",
                        "implementation_session_ids",
                        "implementation_run_ids",
                        "verdict",
                        "resolved_findings",
                        "reviewed_at",
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    """Phase 4 前的最小、可验证命令执行证据。"""

    receipt_id: str
    requirement_id: str
    commit_sha: str
    suite_id: str
    suite_fingerprint: str
    issuer: str
    run_id: str
    session_id: str
    command: str
    environment: str
    started_at: str
    completed_at: str
    exit_code: int
    status: str
    summary: str
    source_url: str | None = None
    schema_version: int = SCHEMA_VERSION
    _extra_fields_json: str = "{}"

    def to_dict(self) -> dict[str, object]:
        return _with_extra(
            self._extra_fields_json,
            {
                "schema_version": self.schema_version,
                "receipt_id": self.receipt_id,
                "requirement_id": self.requirement_id,
                "commit_sha": self.commit_sha,
                "suite_id": self.suite_id,
                "suite_fingerprint": self.suite_fingerprint,
                "issuer": self.issuer,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "command": self.command,
                "environment": self.environment,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "exit_code": self.exit_code,
                "status": self.status,
                "summary": self.summary,
                "source_url": self.source_url,
            },
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise PhaseGateError("VerificationReceipt exit_code 必须是整数")
        return cls(
            receipt_id=_required_string(payload, "receipt_id"),
            requirement_id=_required_string(payload, "requirement_id"),
            commit_sha=_required_string(payload, "commit_sha"),
            suite_id=_required_string(payload, "suite_id"),
            suite_fingerprint=_required_string(payload, "suite_fingerprint"),
            issuer=_required_string(payload, "issuer"),
            run_id=_required_string(payload, "run_id"),
            session_id=_required_string(payload, "session_id"),
            command=_required_string(payload, "command"),
            environment=_required_string(payload, "environment"),
            started_at=_required_string(payload, "started_at"),
            completed_at=_required_string(payload, "completed_at"),
            exit_code=exit_code,
            status=_required_string(payload, "status"),
            summary=_required_string(payload, "summary"),
            source_url=_optional_string(payload, "source_url"),
            schema_version=_schema_version(payload),
            _extra_fields_json=_extra_json(
                payload,
                frozenset(
                    {
                        "schema_version",
                        "receipt_id",
                        "requirement_id",
                        "commit_sha",
                        "suite_id",
                        "suite_fingerprint",
                        "issuer",
                        "run_id",
                        "session_id",
                        "command",
                        "environment",
                        "started_at",
                        "completed_at",
                        "exit_code",
                        "status",
                        "summary",
                        "source_url",
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class VerificationSuiteDefinition:
    """GateDefinition 中由受控执行器解释的验证套件。"""

    suite_id: str
    kind: str
    commands: tuple[tuple[str, ...], ...] = ()
    repository: str | None = None
    workflow: str | None = None
    required_event: str | None = None
    required_jobs: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(self.to_dict())

    @property
    def expected_issuer(self) -> str:
        if self.kind == "command":
            return "workspace-command-runner"
        if self.kind == "github-actions":
            return "github-actions-api"
        raise PhaseGateError(f"不支持 Verification Suite kind={self.kind}")

    @property
    def command_summary(self) -> str:
        if self.kind == "command":
            return json.dumps(self.commands, ensure_ascii=False, separators=(",", ":"))
        return (
            f"github-actions:{self.repository}:{self.workflow}:{self.required_event}:"
            + ",".join(self.required_jobs)
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.suite_id,
            "kind": self.kind,
        }
        if self.kind == "command":
            result["commands"] = [list(command) for command in self.commands]
        elif self.kind == "github-actions":
            result.update(
                {
                    "repository": self.repository,
                    "workflow": self.workflow,
                    "required_event": self.required_event,
                    "required_jobs": list(self.required_jobs),
                }
            )
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        suite_id = _required_string(payload, "id")
        kind = _required_string(payload, "kind")
        if _RECEIPT_ID_PATTERN.fullmatch(suite_id) is None:
            raise PhaseGateError("Verification Suite ID 只能包含安全字符")
        if kind == "command":
            raw_commands = payload.get("commands")
            if not isinstance(raw_commands, list) or not raw_commands:
                raise PhaseGateError("command Verification Suite 必须声明 commands")
            commands: list[tuple[str, ...]] = []
            for raw_command in raw_commands:
                if (
                    not isinstance(raw_command, list)
                    or not raw_command
                    or any(not isinstance(item, str) or not item for item in raw_command)
                ):
                    raise PhaseGateError("Verification Suite command 必须是非空字符串数组")
                commands.append(tuple(raw_command))
            return cls(suite_id=suite_id, kind=kind, commands=tuple(commands))
        if kind == "github-actions":
            required_jobs = _string_tuple(payload, "required_jobs")
            if not required_jobs:
                raise PhaseGateError("github-actions Verification Suite 必须声明 required_jobs")
            if len(required_jobs) != len(set(required_jobs)):
                raise PhaseGateError("github-actions Verification Suite 的 required_jobs 不得重复")
            return cls(
                suite_id=suite_id,
                kind=kind,
                repository=_required_string(payload, "repository"),
                workflow=_required_string(payload, "workflow"),
                required_event=_required_string(payload, "required_event"),
                required_jobs=required_jobs,
            )
        raise PhaseGateError(f"不支持 Verification Suite kind={kind}")


@dataclass(frozen=True, slots=True)
class AcceptanceDefinition:
    """GateDefinition 中带稳定 ID 的验收定义。"""

    acceptance_id: str
    description: str

    def to_dict(self) -> dict[str, object]:
        return {"id": self.acceptance_id, "description": self.description}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            acceptance_id=_required_string(payload, "id"),
            description=_required_string(payload, "description"),
        )


@dataclass(frozen=True, slots=True)
class GateDefinition:
    """从 Git commit 读取的权威阶段计划与验收定义。"""

    requirement_id: str
    phase: int
    task_id: str
    plan_source_path: str
    plan_source_fingerprint: str
    acceptance: tuple[AcceptanceDefinition, ...]
    verification_suites: tuple[VerificationSuiteDefinition, ...]
    next_task_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def acceptance_fingerprint(self) -> str:
        return content_fingerprint([item.to_dict() for item in self.acceptance])

    @property
    def acceptance_ids(self) -> tuple[str, ...]:
        return tuple(item.acceptance_id for item in self.acceptance)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        phase = payload.get("phase")
        if isinstance(phase, bool) or not isinstance(phase, int):
            raise PhaseGateError("GateDefinition phase 必须是整数")
        raw_acceptance = payload.get("acceptance")
        if not isinstance(raw_acceptance, list):
            raise PhaseGateError("GateDefinition acceptance 必须是 JSON 数组")
        raw_suites = payload.get("verification_suites")
        if not isinstance(raw_suites, list):
            raise PhaseGateError("GateDefinition verification_suites 必须是 JSON 数组")
        return cls(
            requirement_id=_required_string(payload, "requirement_id"),
            phase=phase,
            task_id=_required_string(payload, "task_id"),
            plan_source_path=_required_string(payload, "plan_source_path"),
            plan_source_fingerprint=_required_string(payload, "plan_source_fingerprint"),
            acceptance=tuple(
                AcceptanceDefinition.from_dict(
                    _object_mapping(item, "GateDefinition acceptance[]")
                )
                for item in raw_acceptance
            ),
            verification_suites=tuple(
                VerificationSuiteDefinition.from_dict(
                    _object_mapping(item, "GateDefinition verification_suites[]")
                )
                for item in raw_suites
            ),
            next_task_id=_optional_string(payload, "next_task_id"),
            schema_version=_schema_version(payload),
        )

@dataclass(frozen=True, slots=True)
class PhaseGateRecord:
    """一个 Phase 在某个 exact commit 上的不可变、可重放门禁事实。"""

    requirement_id: str
    phase: int
    task_id: str
    commit_sha: str
    plan_fingerprint: str
    acceptance_fingerprint: str
    acceptance_results: tuple[AcceptanceResult, ...]
    verification_receipt_refs: tuple[str, ...]
    verification_receipt_fingerprints: tuple[str, ...]
    regression_summary: str
    review_attestation: ReviewAttestation
    issued_at: str
    issued_by: str
    status: str
    previous_gate_phase: int | None = None
    previous_gate_commit_sha: str | None = None
    previous_gate_record_fingerprint: str | None = None
    schema_version: int = SCHEMA_VERSION
    _extra_fields_json: str = "{}"

    def to_dict(self) -> dict[str, object]:
        return _with_extra(
            self._extra_fields_json,
            {
                "schema_version": self.schema_version,
                "requirement_id": self.requirement_id,
                "phase": self.phase,
                "task_id": self.task_id,
                "commit_sha": self.commit_sha,
                "previous_gate_phase": self.previous_gate_phase,
                "previous_gate_commit_sha": self.previous_gate_commit_sha,
                "previous_gate_record_fingerprint": self.previous_gate_record_fingerprint,
                "plan_fingerprint": self.plan_fingerprint,
                "acceptance_fingerprint": self.acceptance_fingerprint,
                "acceptance_results": [item.to_dict() for item in self.acceptance_results],
                "verification_receipt_refs": list(self.verification_receipt_refs),
                "verification_receipt_fingerprints": list(
                    self.verification_receipt_fingerprints
                ),
                "regression_summary": self.regression_summary,
                "review_attestation": self.review_attestation.to_dict(),
                "issued_at": self.issued_at,
                "issued_by": self.issued_by,
                "status": self.status,
            },
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        phase = payload.get("phase")
        if isinstance(phase, bool) or not isinstance(phase, int):
            raise PhaseGateError("phase 必须是整数")
        raw_results = payload.get("acceptance_results")
        if not isinstance(raw_results, list):
            raise PhaseGateError("acceptance_results 必须是 JSON 数组")
        raw_attestation = _object_mapping(
            payload.get("review_attestation"), "review_attestation"
        )
        return cls(
            requirement_id=_required_string(payload, "requirement_id"),
            phase=phase,
            task_id=_required_string(payload, "task_id"),
            commit_sha=_required_string(payload, "commit_sha"),
            previous_gate_phase=_optional_int(payload, "previous_gate_phase"),
            previous_gate_commit_sha=_optional_string(
                payload, "previous_gate_commit_sha"
            ),
            previous_gate_record_fingerprint=_optional_string(
                payload, "previous_gate_record_fingerprint"
            ),
            plan_fingerprint=_required_string(payload, "plan_fingerprint"),
            acceptance_fingerprint=_required_string(payload, "acceptance_fingerprint"),
            acceptance_results=tuple(
                AcceptanceResult.from_dict(_object_mapping(item, "acceptance_results[]"))
                for item in raw_results
            ),
            verification_receipt_refs=_string_tuple(
                payload, "verification_receipt_refs"
            ),
            verification_receipt_fingerprints=_string_tuple(
                payload, "verification_receipt_fingerprints"
            ),
            regression_summary=_required_string(payload, "regression_summary"),
            review_attestation=ReviewAttestation.from_dict(raw_attestation),
            issued_at=_required_string(payload, "issued_at"),
            issued_by=_required_string(payload, "issued_by"),
            status=_required_string(payload, "status"),
            schema_version=_schema_version(payload),
            _extra_fields_json=_extra_json(
                payload,
                frozenset(
                    {
                        "schema_version",
                        "requirement_id",
                        "phase",
                        "task_id",
                        "commit_sha",
                        "previous_gate_phase",
                        "previous_gate_commit_sha",
                        "previous_gate_record_fingerprint",
                        "plan_fingerprint",
                        "acceptance_fingerprint",
                        "acceptance_results",
                        "verification_receipt_refs",
                        "verification_receipt_fingerprints",
                        "regression_summary",
                        "review_attestation",
                        "issued_at",
                        "issued_by",
                        "status",
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class PhaseActivationRecord:
    """exact-SHA Gate 成功转换为下一阶段执行权的不可变记录。"""

    requirement_id: str
    phase: int
    task_id: str
    predecessor_gate_phase: int
    predecessor_gate_commit_sha: str
    predecessor_gate_record_fingerprint: str
    session_id: str
    activated_at: str
    activated_by: str
    schema_version: int = SCHEMA_VERSION
    _extra_fields_json: str = "{}"

    def to_dict(self) -> dict[str, object]:
        return _with_extra(
            self._extra_fields_json,
            {
                "schema_version": self.schema_version,
                "requirement_id": self.requirement_id,
                "phase": self.phase,
                "task_id": self.task_id,
                "predecessor_gate_phase": self.predecessor_gate_phase,
                "predecessor_gate_commit_sha": self.predecessor_gate_commit_sha,
                "predecessor_gate_record_fingerprint": (
                    self.predecessor_gate_record_fingerprint
                ),
                "session_id": self.session_id,
                "activated_at": self.activated_at,
                "activated_by": self.activated_by,
            },
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        phase = payload.get("phase")
        predecessor_phase = payload.get("predecessor_gate_phase")
        if isinstance(phase, bool) or not isinstance(phase, int):
            raise PhaseGateError("PhaseActivationRecord phase 必须是整数")
        if isinstance(predecessor_phase, bool) or not isinstance(predecessor_phase, int):
            raise PhaseGateError(
                "PhaseActivationRecord predecessor_gate_phase 必须是整数"
            )
        return cls(
            requirement_id=_required_string(payload, "requirement_id"),
            phase=phase,
            task_id=_required_string(payload, "task_id"),
            predecessor_gate_phase=predecessor_phase,
            predecessor_gate_commit_sha=_required_string(
                payload, "predecessor_gate_commit_sha"
            ),
            predecessor_gate_record_fingerprint=_required_string(
                payload, "predecessor_gate_record_fingerprint"
            ),
            session_id=_required_string(payload, "session_id"),
            activated_at=_required_string(payload, "activated_at"),
            activated_by=_required_string(payload, "activated_by"),
            schema_version=_schema_version(payload),
            _extra_fields_json=_extra_json(
                payload,
                frozenset(
                    {
                        "schema_version",
                        "requirement_id",
                        "phase",
                        "task_id",
                        "predecessor_gate_phase",
                        "predecessor_gate_commit_sha",
                        "predecessor_gate_record_fingerprint",
                        "session_id",
                        "activated_at",
                        "activated_by",
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class PhaseReviewRecord:
    """Create-once independent review bound to exact receipts and commit."""

    requirement_id: str
    phase: int
    commit_sha: str
    verification_receipt_refs: tuple[str, ...]
    verification_receipt_fingerprints: tuple[str, ...]
    attestation: ReviewAttestation
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "phase": self.phase,
            "commit_sha": self.commit_sha,
            "verification_receipt_refs": list(self.verification_receipt_refs),
            "verification_receipt_fingerprints": list(
                self.verification_receipt_fingerprints
            ),
            "attestation": self.attestation.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        phase = payload.get("phase")
        if isinstance(phase, bool) or not isinstance(phase, int):
            raise PhaseGateError("PhaseReviewRecord phase 必须是整数")
        return cls(
            requirement_id=_required_string(payload, "requirement_id"),
            phase=phase,
            commit_sha=_required_string(payload, "commit_sha"),
            verification_receipt_refs=_string_tuple(
                payload, "verification_receipt_refs"
            ),
            verification_receipt_fingerprints=_string_tuple(
                payload, "verification_receipt_fingerprints"
            ),
            attestation=ReviewAttestation.from_dict(
                _object_mapping(payload.get("attestation"), "attestation")
            ),
            schema_version=_schema_version(payload),
        )


def gate_record_fingerprint(record: PhaseGateRecord) -> str:
    """生成前序 Gate 引用使用的内容指纹。"""

    return content_fingerprint(record.to_dict())


def verification_receipt_fingerprint(receipt: VerificationReceipt) -> str:
    """Anchor every persisted Receipt field into the immutable Gate record."""

    return content_fingerprint(receipt.to_dict())


def _require_sha(value: str, label: str) -> None:
    if _SHA_PATTERN.fullmatch(value) is None:
        raise PhaseGateError(f"{label} 必须是 40 位小写十六进制 Git SHA")


def _require_fingerprint(value: str, label: str) -> None:
    if _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise PhaseGateError(f"{label} 必须是 64 位小写十六进制 SHA-256")


def _require_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PhaseGateError(f"{label} 必须是有效 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PhaseGateError(f"{label} 必须包含时区")
    return parsed


def _validate_verification_receipt(receipt: VerificationReceipt) -> None:
    if receipt.schema_version != SCHEMA_VERSION:
        raise PhaseGateError(
            f"不支持 VerificationReceipt schema_version={receipt.schema_version}"
        )
    if _RECEIPT_ID_PATTERN.fullmatch(receipt.receipt_id) is None:
        raise PhaseGateError("VerificationReceipt receipt_id 只能包含安全文件名字符")
    if re.fullmatch(r"REQ-\d{3,}", receipt.requirement_id) is None:
        raise PhaseGateError("VerificationReceipt Requirement ID 无效")
    _require_sha(receipt.commit_sha, "VerificationReceipt commit_sha")
    _require_fingerprint(
        receipt.suite_fingerprint, "VerificationReceipt suite_fingerprint"
    )
    if any(
        not value.strip()
        for value in (
            receipt.run_id,
            receipt.session_id,
            receipt.suite_id,
            receipt.issuer,
            receipt.command,
            receipt.environment,
            receipt.summary,
        )
    ):
        raise PhaseGateError("VerificationReceipt 的 run/session/command/environment/summary 不能为空")
    started_at = _require_timestamp(receipt.started_at, "VerificationReceipt started_at")
    completed_at = _require_timestamp(receipt.completed_at, "VerificationReceipt completed_at")
    if completed_at < started_at:
        raise PhaseGateError("VerificationReceipt completed_at 不能早于 started_at")
    if receipt.status != "PASS" or receipt.exit_code != 0:
        raise PhaseGateError(
            f"VerificationReceipt 未通过：status={receipt.status}, exit_code={receipt.exit_code}"
        )


def _validate_pass_facts(record: PhaseGateRecord) -> None:
    if record.schema_version != SCHEMA_VERSION:
        raise PhaseGateError(
            f"不支持 PhaseGateRecord schema_version={record.schema_version}"
        )
    if record.phase < 0:
        raise PhaseGateError("phase 不能小于 0")
    if re.fullmatch(r"REQ-\d{3,}", record.requirement_id) is None:
        raise PhaseGateError(f"无效的 Requirement ID：{record.requirement_id}")
    if not record.task_id.strip():
        raise PhaseGateError("task_id 不能为空")
    if not record.regression_summary.strip():
        raise PhaseGateError("regression_summary 不能为空")
    if not record.issued_at.strip() or not record.issued_by.strip():
        raise PhaseGateError("issued_at 与 issued_by 不能为空")
    issued_at = _require_timestamp(record.issued_at, "issued_at")
    if issued_at > datetime.now(issued_at.tzinfo) + timedelta(minutes=5):
        raise PhaseGateError("issued_at 不能是未来时间")
    _require_sha(record.commit_sha, "commit_sha")
    _require_fingerprint(record.plan_fingerprint, "plan_fingerprint")
    _require_fingerprint(record.acceptance_fingerprint, "acceptance_fingerprint")
    if record.status != "PASS":
        raise PhaseGateError(f"Phase Gate 未通过：{record.status}")
    if not record.acceptance_results:
        raise PhaseGateError("Phase Gate 至少需要一个 AcceptanceResult")
    acceptance_ids = [item.acceptance_id for item in record.acceptance_results]
    if any(not item.strip() for item in acceptance_ids):
        raise PhaseGateError("Acceptance ID 不能为空")
    duplicates = sorted(item for item, count in Counter(acceptance_ids).items() if count > 1)
    if duplicates:
        raise PhaseGateError("Acceptance ID 重复：" + ", ".join(duplicates))
    failed = [item.acceptance_id for item in record.acceptance_results if item.status != "PASS"]
    if failed:
        raise PhaseGateError("Acceptance 未全部 PASS：" + ", ".join(failed))
    if any(item.schema_version != SCHEMA_VERSION for item in record.acceptance_results):
        raise PhaseGateError("不支持 AcceptanceResult schema_version")
    if any(not item.summary.strip() for item in record.acceptance_results):
        raise PhaseGateError("AcceptanceResult summary 不能为空")
    if any(not item.evidence_refs for item in record.acceptance_results):
        raise PhaseGateError("每个 AcceptanceResult 必须引用至少一项证据")
    if any(
        not evidence.strip()
        for item in record.acceptance_results
        for evidence in item.evidence_refs
    ):
        raise PhaseGateError("AcceptanceResult 证据引用不能为空")
    if not record.verification_receipt_refs:
        raise PhaseGateError("PASS Gate 必须引用至少一个 Verification Receipt")
    if any(not item.strip() for item in record.verification_receipt_refs):
        raise PhaseGateError("Verification Receipt 引用不能为空")
    if len(record.verification_receipt_fingerprints) != len(
        record.verification_receipt_refs
    ):
        raise PhaseGateError("Verification Receipt 引用与内容指纹数量不一致")
    for fingerprint in record.verification_receipt_fingerprints:
        _require_fingerprint(fingerprint, "Verification Receipt fingerprint")
    review = record.review_attestation
    if review.schema_version != SCHEMA_VERSION:
        raise PhaseGateError("不支持 ReviewAttestation schema_version")
    if review.verdict != "PASS":
        raise PhaseGateError(f"独立 Review 未通过：{review.verdict}")
    if not review.reviewer_session_id.strip():
        raise PhaseGateError("Reviewer Session 不能为空")
    if not review.implementation_session_ids:
        raise PhaseGateError("至少需要一个实现 Session")
    if any(not value.strip() for value in review.implementation_session_ids):
        raise PhaseGateError("实现 Session ID 不能为空")
    if review.reviewer_session_id in review.implementation_session_ids:
        raise PhaseGateError("Reviewer Session 不能参与本阶段实现")
    if len(set(review.implementation_session_ids)) != len(review.implementation_session_ids):
        raise PhaseGateError("实现 Session ID 不能重复")
    if not review.implementation_run_ids:
        raise PhaseGateError("至少需要一个实现 Run ID")
    if any(not value.strip() for value in review.implementation_run_ids):
        raise PhaseGateError("实现 Run ID 不能为空")
    if len(set(review.implementation_run_ids)) != len(review.implementation_run_ids):
        raise PhaseGateError("实现 Run ID 不能重复")
    reviewed_at = _require_timestamp(review.reviewed_at, "reviewed_at")
    if reviewed_at > issued_at:
        raise PhaseGateError("reviewed_at 不能晚于 Gate issued_at")


class GateStore:
    """在 Requirement 可选目录中原子签发并读取 Phase Gate。"""

    def __init__(
        self,
        workspace_store: WorkspaceStore,
        git: GitRevisionReader | None = None,
    ) -> None:
        self.workspace_store = workspace_store
        self.git = git or LocalGitProvider(workspace_store.working_root)

    def path_for(self, requirement_id: str, phase: int) -> Path:
        if phase < 0:
            raise PhaseGateError("phase 不能小于 0")
        return self.workspace_store.path_for(requirement_id) / "phase-gates" / f"phase-{phase}.json"

    def receipt_path_for(self, requirement_id: str, receipt_id: str) -> Path:
        if _RECEIPT_ID_PATTERN.fullmatch(receipt_id) is None:
            raise PhaseGateError("Verification Receipt ID 只能包含安全文件名字符")
        return (
            self.workspace_store.path_for(requirement_id)
            / "verification-receipts"
            / f"{receipt_id}.json"
        )

    def activation_path_for(self, requirement_id: str, phase: int) -> Path:
        if phase < 1:
            raise PhaseGateError("只有 Phase 1 及以后需要 Activation Record")
        return (
            self.workspace_store.path_for(requirement_id)
            / "phase-activations"
            / f"phase-{phase}.json"
        )

    def review_path_for(self, requirement_id: str, phase: int) -> Path:
        if phase < 0:
            raise PhaseGateError("phase 不能小于 0")
        return (
            self.workspace_store.path_for(requirement_id)
            / "phase-reviews"
            / f"phase-{phase}.json"
        )

    def reopen_path_for(self, requirement_id: str, phase: int) -> Path:
        if phase < 0:
            raise PhaseGateError("phase 不能小于 0")
        return (
            self.workspace_store.path_for(requirement_id)
            / "phase-reopens"
            / f"phase-{phase}.json"
        )

    def _read_reopen(self, requirement_id: str, phase: int) -> dict[str, object] | None:
        path = self.reopen_path_for(requirement_id, phase)
        if not path.is_file():
            return None
        journal = dict(_object_mapping(self.workspace_store.read_json(path), str(path)))
        archive = _object_mapping(journal.get("archive"), "reopen archive")
        if (
            _schema_version(journal) != SCHEMA_VERSION
            or journal.get("status") not in {"pending", "completed"}
            or archive.get("requirement_id") != requirement_id.upper()
            or archive.get("phase") != phase
            or journal.get("archive_fingerprint") != content_fingerprint(archive)
        ):
            raise PhaseGateError("Phase reopen journal 身份、状态或归档指纹不匹配")
        _required_string(archive, "reason")
        _required_string(archive, "reopened_by")
        _required_string(archive, "task_id")
        _require_timestamp(_required_string(archive, "reopened_at"), "reopened_at")
        return journal

    def _require_no_pending_reopen(self, requirement_id: str, phase: int) -> None:
        journal = self._read_reopen(requirement_id, phase)
        if journal is not None and journal["status"] == "pending":
            raise PhaseGateError(f"Phase {phase} 重审事务未完成，请重试 phase reopen")

    def reopen(
        self,
        requirement_id: str,
        phase: int,
        *,
        reason: str,
        session_id: str,
    ) -> dict[str, object]:
        """保全尚未推进的候选并开放重审；journal 支持崩溃后的幂等恢复。"""

        normalized = requirement_id.upper()
        if not reason.strip() or not session_id.strip():
            raise PhaseGateError("阶段重审必须提供原因并绑定当前 Session")
        with self.workspace_store.locked(normalized):
            self._require_workspace(normalized)
            self._require_execution_context(normalized)
            if not self.is_required(normalized):
                raise PhaseGateError("只有启用阶段门禁的 Requirement 可以重审")
            definitions = self.definitions(normalized)
            if phase < 0 or phase >= len(definitions):
                raise PhaseGateError(f"Phase {phase} 未在完整 GateDefinition 链中声明")
            definition = definitions[phase]
            meta = self.workspace_store.load(normalized)["meta"]
            if self.activation_path_for(normalized, phase + 1).exists():
                raise PhaseGateError("后序 Phase 已激活，不可重审其引用的 Gate")
            if (
                meta.get("requirement_task_id") != definition.task_id
                or meta.get("status") == "done"
            ):
                raise PhaseGateError("只能重审 Requirement 当前未完成的 Phase Task")
            if phase > 0:
                self._require_phase_activation(
                    normalized, phase, definition, require_current=True
                )
            paths = {
                "review": self.review_path_for(normalized, phase),
                "gate": self.path_for(normalized, phase),
            }
            candidates = {
                name: self.workspace_store.read_json(path) if path.is_file() else None
                for name, path in paths.items()
            }
            journal = self._read_reopen(normalized, phase)
            if journal is not None and journal["status"] == "pending":
                archive = _object_mapping(journal["archive"], "reopen archive")
                if archive["reason"] != reason.strip():
                    raise PhaseGateError("未完成的 reopen 必须使用原原因恢复")
                if archive["task_id"] != definition.task_id:
                    raise PhaseGateError("重审事务的 Phase Task 已变化")
            elif not any(value is not None for value in candidates.values()):
                if journal is not None:
                    archive = _object_mapping(journal["archive"], "reopen archive")
                    if archive["reason"] != reason.strip():
                        raise PhaseGateError("当前 Phase 没有可重审的 Review 或 Gate")
                else:
                    raise PhaseGateError("当前 Phase 没有可重审的 Review 或 Gate")
            else:
                archive = {
                    "schema_version": SCHEMA_VERSION,
                    "requirement_id": normalized,
                    "phase": phase,
                    "task_id": definition.task_id,
                    "reason": reason.strip(),
                    "reopened_by": session_id,
                    "reopened_at": now_iso(),
                    **candidates,
                }
                journal = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "pending",
                    "archive_fingerprint": content_fingerprint(archive),
                    "archive": archive,
                }
                self.workspace_store.write_json(
                    self.reopen_path_for(normalized, phase), journal
                )
            # 先持久化完整 journal，再写不可变归档，最后移除当前候选引用。
            # 任意边界崩溃都能由原 journal 恢复，Receipt 始终保留原文件。
            archive_path = (
                self.workspace_store.path_for(normalized)
                / "phase-history"
                / f"phase-{phase}"
                / f"{journal['archive_fingerprint']}.json"
            )
            if archive_path.is_file():
                if self.workspace_store.read_json(archive_path) != archive:
                    raise PhaseGateError("Phase 重审历史归档已存在且不可覆盖")
            else:
                self.workspace_store.write_json(archive_path, archive)
            for name, candidate in candidates.items():
                if candidate is not None and candidate != archive.get(name):
                    raise PhaseGateError("重审恢复期间当前候选已变化，拒绝移除")
            for path in paths.values():
                path.unlink(missing_ok=True)
            completed = {**journal, "status": "completed"}
            self.workspace_store.write_json(
                self.reopen_path_for(normalized, phase), completed
            )
            return completed

    @staticmethod
    def definition_path(requirement_id: str, phase: int) -> str:
        normalized = requirement_id.upper()
        if re.fullmatch(r"REQ-\d{3,}", normalized) is None or phase < 0:
            raise PhaseGateError("GateDefinition 的 Requirement ID 或 phase 无效")
        return f".ai-dev-os/gate-definitions/{normalized}/phase-{phase}.json"

    def load_definition(
        self,
        requirement_id: str,
        phase: int,
        *,
        revision: str = "HEAD",
    ) -> GateDefinition:
        path = self.definition_path(requirement_id, phase)
        try:
            payload = json.loads(self.git.read_file_at(revision, path))
        except (json.JSONDecodeError, GitError, OSError, RuntimeError) as exc:
            raise PhaseGateError(f"无法从 {revision} 读取 GateDefinition {path}：{exc}") from exc
        definition = GateDefinition.from_dict(_object_mapping(payload, path))
        if definition.schema_version != SCHEMA_VERSION:
            raise PhaseGateError(
                f"不支持 GateDefinition schema_version={definition.schema_version}"
            )
        if definition.requirement_id != requirement_id.upper() or definition.phase != phase:
            raise PhaseGateError("GateDefinition 的 Requirement/phase 与文件路径不一致")
        if not definition.acceptance:
            raise PhaseGateError("GateDefinition acceptance 不能为空")
        duplicates = sorted(
            item
            for item, count in Counter(definition.acceptance_ids).items()
            if count > 1
        )
        if duplicates:
            raise PhaseGateError("GateDefinition Acceptance ID 重复：" + ", ".join(duplicates))
        if not definition.verification_suites:
            raise PhaseGateError("GateDefinition verification_suites 不能为空")
        suite_ids = [suite.suite_id for suite in definition.verification_suites]
        duplicate_suites = sorted(
            item for item, count in Counter(suite_ids).items() if count > 1
        )
        if duplicate_suites:
            raise PhaseGateError(
                "GateDefinition Verification Suite ID 重复："
                + ", ".join(duplicate_suites)
            )
        _require_fingerprint(
            definition.plan_source_fingerprint, "GateDefinition plan_source_fingerprint"
        )
        try:
            plan_source = self.git.read_file_at(revision, definition.plan_source_path)
        except (GitError, OSError, RuntimeError) as exc:
            raise PhaseGateError(
                f"无法从 {revision} 读取权威计划 {definition.plan_source_path}：{exc}"
            ) from exc
        if source_fingerprint(plan_source) != definition.plan_source_fingerprint:
            raise PhaseGateError("GateDefinition 的计划源 digest 与已提交文件不一致")
        return definition

    def definitions(self, requirement_id: str, *, revision: str = "HEAD") -> tuple[GateDefinition, ...]:
        """返回某个 Requirement 在已提交 Git 树中的连续阶段定义。"""

        normalized = requirement_id.upper()
        prefix = f".ai-dev-os/gate-definitions/{normalized}/"
        try:
            paths = self.git.list_files_at(revision, prefix)
        except (GitError, OSError, RuntimeError) as exc:
            raise PhaseGateError(f"无法枚举 {revision} 的 GateDefinition：{exc}") from exc
        phases: list[int] = []
        for path in paths:
            match = re.fullmatch(re.escape(prefix) + r"phase-(\d+)\.json", path)
            if match:
                phases.append(int(match.group(1)))
        if not phases:
            return ()
        ordered = sorted(set(phases))
        if ordered != list(range(ordered[-1] + 1)):
            raise PhaseGateError("GateDefinition phase 必须从 0 连续编号")
        definitions = tuple(
            self.load_definition(normalized, phase, revision=revision) for phase in ordered
        )
        task_ids = [definition.task_id for definition in definitions]
        duplicate_tasks = sorted(
            task_id for task_id, count in Counter(task_ids).items() if count > 1
        )
        if duplicate_tasks:
            raise PhaseGateError(
                "GateDefinition Phase Task ID 重复：" + ", ".join(duplicate_tasks)
            )
        for current, following in pairwise(definitions):
            if current.next_task_id != following.task_id:
                raise PhaseGateError(
                    f"Phase {current.phase} next_task_id 未精确指向 "
                    f"Phase {following.phase} Task"
                )
        if definitions[-1].next_task_id is not None:
            raise PhaseGateError("最终 GateDefinition 仍声明后序 Task，阶段链不闭合")
        return definitions

    def read(self, requirement_id: str, phase: int) -> PhaseGateRecord:
        path = self.path_for(requirement_id, phase)
        if not path.is_file():
            raise PhaseGateError(f"缺少前序 Phase Gate：{path}")
        payload = self.workspace_store.read_json(path)
        return PhaseGateRecord.from_dict(_object_mapping(payload, str(path)))

    def read_verification_receipt(
        self, requirement_id: str, receipt_id: str
    ) -> VerificationReceipt:
        path = self.receipt_path_for(requirement_id, receipt_id)
        if not path.is_file():
            raise PhaseGateError(f"缺少 Verification Receipt：{receipt_id}")
        payload = self.workspace_store.read_json(path)
        receipt = VerificationReceipt.from_dict(_object_mapping(payload, str(path)))
        _validate_verification_receipt(receipt)
        if receipt.receipt_id != receipt_id:
            raise PhaseGateError("Verification Receipt ID 与文件名不一致")
        if receipt.requirement_id != requirement_id.upper():
            raise PhaseGateError("Verification Receipt 属于另一个 Requirement")
        return receipt

    def _write_verification_receipt(self, receipt: VerificationReceipt) -> Path:
        _validate_verification_receipt(receipt)
        with self.workspace_store.locked(receipt.requirement_id):
            self._require_workspace(receipt.requirement_id)
            path = self.receipt_path_for(receipt.requirement_id, receipt.receipt_id)
            if path.is_file():
                existing = self.read_verification_receipt(
                    receipt.requirement_id, receipt.receipt_id
                )
                if existing == receipt:
                    return path
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 已存在且不可覆盖"
                )
            self.workspace_store.write_json(path, receipt.to_dict())
            return path

    def read_review(self, requirement_id: str, phase: int) -> PhaseReviewRecord:
        path = self.review_path_for(requirement_id, phase)
        if not path.is_file():
            raise PhaseGateError(f"缺少 Phase {phase} 独立 Review Record")
        payload = self.workspace_store.read_json(path)
        record = PhaseReviewRecord.from_dict(_object_mapping(payload, str(path)))
        if record.requirement_id != requirement_id.upper() or record.phase != phase:
            raise PhaseGateError("PhaseReviewRecord 与路径身份不一致")
        return record

    def record_review_from_payload(
        self,
        requirement_id: str,
        phase: int,
        payload: Mapping[str, object],
        *,
        reviewer_session_id: str,
    ) -> PhaseReviewRecord:
        """Create an exact-SHA review record; identity/time are runtime-derived."""

        normalized = requirement_id.upper()
        if not reviewer_session_id.strip():
            raise PhaseGateError("Reviewer Session 不能为空")
        commit_sha = self.git.head_sha()
        definitions = self.definitions(normalized, revision=commit_sha)
        if phase >= len(definitions):
            raise PhaseGateError(f"Phase {phase} 未在完整 GateDefinition 链中声明")
        definition = definitions[phase]
        receipt_refs = _string_tuple(payload, "verification_receipt_refs")
        receipts = tuple(
            self.read_verification_receipt(normalized, receipt_id)
            for receipt_id in receipt_refs
        )
        attestation = ReviewAttestation(
            reviewer_session_id=reviewer_session_id,
            implementation_session_ids=_string_tuple(
                payload, "implementation_session_ids"
            ),
            implementation_run_ids=_string_tuple(payload, "implementation_run_ids"),
            verdict=_required_string(payload, "verdict"),
            resolved_findings=_string_tuple(payload, "resolved_findings"),
            reviewed_at=now_iso(),
        )
        fingerprints = tuple(
            verification_receipt_fingerprint(receipt) for receipt in receipts
        )
        evidence = receipt_refs[0] if receipt_refs else "missing"
        provisional = PhaseGateRecord(
            requirement_id=normalized,
            phase=phase,
            task_id=definition.task_id,
            commit_sha=commit_sha,
            plan_fingerprint=definition.plan_source_fingerprint,
            acceptance_fingerprint=definition.acceptance_fingerprint,
            acceptance_results=tuple(
                AcceptanceResult(item.acceptance_id, "PASS", "reviewed", (evidence,))
                for item in definition.acceptance
            ),
            verification_receipt_refs=receipt_refs,
            verification_receipt_fingerprints=fingerprints,
            regression_summary="independent review input validation",
            review_attestation=attestation,
            issued_at=attestation.reviewed_at,
            issued_by=reviewer_session_id,
            status="PASS",
        )
        _validate_pass_facts(provisional)
        self._require_exact_head(provisional)
        not_before = None
        if phase > 0:
            activation = self._require_phase_activation(
                normalized, phase, definition, require_current=True
            )
            not_before = _require_timestamp(activation.activated_at, "activated_at")
        self._validate_receipt_bindings(
            provisional, definition, not_before=not_before
        )
        record = PhaseReviewRecord(
            requirement_id=normalized,
            phase=phase,
            commit_sha=commit_sha,
            verification_receipt_refs=receipt_refs,
            verification_receipt_fingerprints=fingerprints,
            attestation=attestation,
        )
        with self.workspace_store.locked(normalized):
            path = self.review_path_for(normalized, phase)
            if path.is_file():
                existing = self.read_review(normalized, phase)
                retried = replace(
                    record,
                    attestation=replace(
                        record.attestation,
                        reviewed_at=existing.attestation.reviewed_at,
                    ),
                )
                if existing == retried:
                    self._require_exact_head(provisional)
                    return existing
                raise PhaseGateError(
                    f"Phase {phase} 独立 Review 已存在且不可覆盖；请先 phase reopen"
                )
            self._require_exact_head(provisional)
            self.workspace_store.write_json(path, record.to_dict())
        return record

    def verification_suite(
        self,
        requirement_id: str,
        phase: int,
        suite_id: str,
        *,
        revision: str = "HEAD",
    ) -> VerificationSuiteDefinition:
        definition = self.load_definition(requirement_id, phase, revision=revision)
        suite = next(
            (item for item in definition.verification_suites if item.suite_id == suite_id),
            None,
        )
        if suite is None:
            raise PhaseGateError(f"Phase {phase} 未声明 Verification Suite {suite_id}")
        return suite

    def read_activation(self, requirement_id: str, phase: int) -> PhaseActivationRecord:
        path = self.activation_path_for(requirement_id, phase)
        if not path.is_file():
            raise PhaseGateError(f"缺少 Phase {phase} Activation Record")
        payload = self.workspace_store.read_json(path)
        record = PhaseActivationRecord.from_dict(_object_mapping(payload, str(path)))
        if record.requirement_id != requirement_id.upper() or record.phase != phase:
            raise PhaseGateError("PhaseActivationRecord 与路径身份不一致")
        return record

    def _write_activation(self, record: PhaseActivationRecord) -> Path:
        with self.workspace_store.locked(record.requirement_id):
            self._require_workspace(record.requirement_id)
            self._validate_activation(record)
            path = self.activation_path_for(record.requirement_id, record.phase)
            if path.is_file():
                existing = self.read_activation(record.requirement_id, record.phase)
                if existing == record:
                    return path
                raise PhaseGateError(
                    f"Phase {record.phase} Activation Record 已存在且不可覆盖"
                )
            self.workspace_store.write_json(path, record.to_dict())
            return path

    def validate_activation(self, record: PhaseActivationRecord) -> None:
        with self.workspace_store.locked(record.requirement_id):
            self._require_workspace(record.requirement_id)
            self._validate_activation(record)

    def require_task_active(self, requirement_id: str, task_id: str) -> str:
        """返回 unmanaged/initial/activated；任何阶段任务绕过都 fail closed。"""

        normalized = requirement_id.upper()
        meta = self.workspace_store.load(normalized)["meta"]
        if not self.is_required(normalized):
            return "unmanaged"
        definitions = self.definitions(normalized)
        if not definitions:
            raise PhaseGateError(
                f"{normalized} 已启用阶段门禁，但 HEAD 中没有 GateDefinition"
            )
        declared = any(definition.task_id == task_id for definition in definitions)
        if not declared:
            raise PhaseGateError(
                f"{normalized} 已启用阶段门禁，Task {task_id} 未在 GateDefinition 链中声明"
            )
        if task_id == definitions[0].task_id:
            if meta.get("requirement_task_id") != task_id:
                raise PhaseGateError("Phase 0 Task 不是 Requirement 当前激活任务")
            return "initial"
        predecessor = next(
            (definition for definition in definitions if definition.next_task_id == task_id),
            None,
        )
        if predecessor is None:
            raise PhaseGateError(f"Phase Task {task_id} 缺少直接前序 GateDefinition")
        activation = self.read_activation(normalized, predecessor.phase + 1)
        self._validate_activation(activation)
        if activation.task_id != task_id:
            raise PhaseGateError("Activation Record 与当前 Task 不一致")
        if meta.get("requirement_task_id") != task_id:
            raise PhaseGateError("Phase Task 未经过渡命令 CAS 激活")
        return "activated"

    def is_required(self, requirement_id: str) -> bool:
        """Read the opt-in flag without allowing malformed values to disable gates."""

        meta = self.workspace_store.load(requirement_id.upper())["meta"]
        value = meta.get("phase_gate_required")
        if value is None:
            return False
        if not isinstance(value, bool):
            raise PhaseGateError("phase_gate_required 必须是布尔值")
        return value

    def require_requirement_completion_ready(self, requirement_id: str) -> None:
        """A gated Requirement may complete only after its final exact-SHA Gate."""

        normalized = requirement_id.upper()
        if not self.is_required(normalized):
            return
        definitions = self.definitions(normalized)
        if not definitions:
            raise PhaseGateError(f"{normalized} 缺少 GateDefinition")
        final_definition = definitions[-1]
        if final_definition.next_task_id is not None:
            raise PhaseGateError("最终 GateDefinition 仍声明后序 Task，阶段链不闭合")
        meta = self.workspace_store.load(normalized)["meta"]
        if meta.get("requirement_task_id") != final_definition.task_id:
            raise PhaseGateError("最终 Phase Task 尚未激活，不能完成 Requirement")
        final_gate = self.read(normalized, final_definition.phase)
        self.validate(final_gate)
        if final_gate.task_id != final_definition.task_id:
            raise PhaseGateError("最终 Phase Gate 与最终 Task 不一致")

    def _write_gate_record(
        self,
        record: PhaseGateRecord,
    ) -> Path:
        """验证当前事实后以原子替换写入唯一的 Phase Gate 路径。"""

        with self.workspace_store.locked(record.requirement_id):
            self._require_workspace(record.requirement_id)
            self._validate(record)
            path = self.path_for(record.requirement_id, record.phase)
            if path.is_file():
                existing = self.read(record.requirement_id, record.phase)
                if existing == record:
                    return path
                raise PhaseGateError(
                    f"Phase {record.phase} Gate 已签发且不可覆盖；必须显式保留旧记录"
                )
            self.workspace_store.write_json(path, record.to_dict())
            return path

    def _issue_validated(
        self,
        *,
        requirement_id: str,
        phase: int,
        acceptance_results: tuple[AcceptanceResult, ...],
        verification_receipt_refs: tuple[str, ...],
        regression_summary: str,
        review_attestation: ReviewAttestation,
        issued_by: str,
        issued_at: str | None = None,
    ) -> PhaseGateRecord:
        """从当前 HEAD 和持久化前序 Gate 确定性生成并写入 PASS 记录。"""

        commit_sha = self.git.head_sha()
        definition = self.load_definition(requirement_id, phase, revision=commit_sha)
        previous = self.read(requirement_id, phase - 1) if phase > 0 else None
        receipts = tuple(
            self.read_verification_receipt(requirement_id, receipt_id)
            for receipt_id in verification_receipt_refs
        )
        record = PhaseGateRecord(
            requirement_id=requirement_id.upper(),
            phase=phase,
            task_id=definition.task_id,
            commit_sha=commit_sha,
            previous_gate_phase=previous.phase if previous else None,
            previous_gate_commit_sha=previous.commit_sha if previous else None,
            previous_gate_record_fingerprint=(
                gate_record_fingerprint(previous) if previous else None
            ),
            plan_fingerprint=definition.plan_source_fingerprint,
            acceptance_fingerprint=definition.acceptance_fingerprint,
            acceptance_results=acceptance_results,
            verification_receipt_refs=verification_receipt_refs,
            verification_receipt_fingerprints=tuple(
                verification_receipt_fingerprint(receipt) for receipt in receipts
            ),
            regression_summary=regression_summary,
            review_attestation=review_attestation,
            issued_at=issued_at or now_iso(),
            issued_by=issued_by,
            status="PASS",
        )
        if issued_at is None and self.path_for(requirement_id, phase).is_file():
            existing = self.read(requirement_id, phase)
            if replace(record, issued_at=existing.issued_at) == existing:
                self._write_gate_record(existing)
                return existing
        self._write_gate_record(record)
        return record

    def issue_from_payload(
        self,
        requirement_id: str,
        phase: int,
        payload: Mapping[str, object],
        *,
        issued_by: str,
    ) -> PhaseGateRecord:
        """从 CLI 读取的 JSON packet 签发 Gate，权威定义仍来自 Git。"""

        raw_results = payload.get("acceptance_results")
        if not isinstance(raw_results, list):
            raise PhaseGateError("issue packet acceptance_results 必须是 JSON 数组")
        forbidden = {
            "verification_receipt_refs",
            "review_attestation",
            "issued_at",
            "issued_by",
        }.intersection(payload)
        if forbidden:
            raise PhaseGateError(
                "Gate issue packet 不得自报 Receipt、Reviewer 或签发身份："
                + ", ".join(sorted(forbidden))
            )
        normalized = requirement_id.upper()
        with self.workspace_store.locked(normalized):
            self._require_workspace(normalized)
            self._require_no_pending_reopen(normalized, phase)
            review = self.read_review(normalized, phase)
            if review.schema_version != SCHEMA_VERSION:
                raise PhaseGateError(
                    f"不支持 PhaseReviewRecord schema_version={review.schema_version}"
                )
            _require_sha(review.commit_sha, "PhaseReviewRecord commit_sha")
            if review.commit_sha != self.git.head_sha():
                raise PhaseGateError("独立 Review 未绑定当前 exact SHA")
            if len(review.verification_receipt_refs) != len(
                review.verification_receipt_fingerprints
            ):
                raise PhaseGateError("独立 Review 的 Receipt 引用与指纹数量不一致")
            if len(set(review.verification_receipt_refs)) != len(
                review.verification_receipt_refs
            ):
                raise PhaseGateError("独立 Review 的 Receipt 引用不能重复")
            receipts = tuple(
                self.read_verification_receipt(normalized, receipt_id)
                for receipt_id in review.verification_receipt_refs
            )
            current_receipt_fingerprints = tuple(
                verification_receipt_fingerprint(receipt) for receipt in receipts
            )
            if current_receipt_fingerprints != review.verification_receipt_fingerprints:
                raise PhaseGateError("独立 Review 后 Verification Receipt 内容已变化")
            reviewed_definition = self.load_definition(
                normalized, phase, revision=review.commit_sha
            )
            # A stored PASS receipt is not authority by itself. Re-run local suites and
            # re-query the exact remote CI run immediately before minting the Gate.
            from .phase_verification import PhaseVerificationRunner

            verifier = PhaseVerificationRunner(self)
            for receipt in receipts:
                verifier.revalidate(
                    normalized,
                    phase=phase,
                    receipt=receipt,
                )
            if self.read_review(normalized, phase) != review:
                raise PhaseGateError("Gate 签发重验期间独立 Review 内容已变化")
            refreshed_receipts = tuple(
                self.read_verification_receipt(normalized, receipt_id)
                for receipt_id in review.verification_receipt_refs
            )
            if tuple(
                verification_receipt_fingerprint(receipt)
                for receipt in refreshed_receipts
            ) != review.verification_receipt_fingerprints:
                raise PhaseGateError("Gate 签发重验期间 Verification Receipt 内容已变化")
            if (
                self.git.head_sha() != review.commit_sha
                or not self.git.is_clean()
                or self.load_definition(
                    normalized, phase, revision=review.commit_sha
                )
                != reviewed_definition
            ):
                raise PhaseGateError("Gate 签发重验期间 HEAD、工作树或 GateDefinition 已变化")
            return self._issue_validated(
                requirement_id=normalized,
                phase=phase,
                acceptance_results=tuple(
                    AcceptanceResult.from_dict(
                        _object_mapping(item, "acceptance_results[]")
                    )
                    for item in raw_results
                ),
                verification_receipt_refs=review.verification_receipt_refs,
                regression_summary=_required_string(payload, "regression_summary"),
                review_attestation=review.attestation,
                issued_by=issued_by,
            )

    def validate(
        self,
        record: PhaseGateRecord,
    ) -> None:
        """公开的 fail-closed 校验入口；成功时不修改任何持久状态。"""

        with self.workspace_store.locked(record.requirement_id):
            self._require_workspace(record.requirement_id)
            self._validate(record)

    def _require_workspace(self, requirement_id: str) -> None:
        try:
            self.workspace_store.load(requirement_id)
        except WorkspaceError as exc:
            raise PhaseGateError(str(exc)) from exc

    def _validate(
        self,
        record: PhaseGateRecord,
    ) -> None:
        _validate_pass_facts(record)
        self._require_exact_head(record)
        definitions = self.definitions(record.requirement_id, revision=record.commit_sha)
        if record.phase >= len(definitions):
            raise PhaseGateError(
                f"Phase {record.phase} 未在完整 GateDefinition 链中声明"
            )
        definition = definitions[record.phase]
        if record.task_id != definition.task_id:
            raise PhaseGateError("Phase Gate task_id 与权威 GateDefinition 不一致")
        if record.plan_fingerprint != definition.plan_source_fingerprint:
            raise PhaseGateError("计划内容已变化，plan_fingerprint 已失效")
        if record.acceptance_fingerprint != definition.acceptance_fingerprint:
            raise PhaseGateError("验收定义已变化，acceptance_fingerprint 已失效")
        expected_ids = set(definition.acceptance_ids)
        result_ids = {item.acceptance_id for item in record.acceptance_results}
        if expected_ids != result_ids:
            missing = sorted(expected_ids - result_ids)
            unexpected = sorted(result_ids - expected_ids)
            details: list[str] = []
            if missing:
                details.append("缺失 " + ", ".join(missing))
            if unexpected:
                details.append("多出 " + ", ".join(unexpected))
            raise PhaseGateError("AcceptanceResult 未精确覆盖验收定义：" + "；".join(details))
        if record.phase == 0:
            if any(
                value is not None
                for value in (
                    record.previous_gate_phase,
                    record.previous_gate_commit_sha,
                    record.previous_gate_record_fingerprint,
                )
            ):
                raise PhaseGateError("Phase 0 不能引用前序 Gate")
            self._validate_receipt_bindings(record, definition)
        else:
            previous = self.read(record.requirement_id, record.phase - 1)
            if previous.requirement_id != record.requirement_id:
                raise PhaseGateError("前序 Gate 属于另一个 Requirement")
            if previous.phase != record.phase - 1:
                raise PhaseGateError("前序 Gate 文件中的 phase 与路径不一致")
            self._validate_predecessor_chain(previous)
            if record.previous_gate_phase != previous.phase:
                raise PhaseGateError("previous_gate_phase 未引用直接前序 Phase")
            if record.previous_gate_commit_sha != previous.commit_sha:
                raise PhaseGateError("previous_gate_commit_sha 与前序 Gate 不一致")
            expected_reference = gate_record_fingerprint(previous)
            if record.previous_gate_record_fingerprint != expected_reference:
                raise PhaseGateError(
                    "previous_gate_record_fingerprint 与前序 Gate 不一致"
                )
            if not self.git.is_ancestor(previous.commit_sha, record.commit_sha):
                raise PhaseGateError("当前 Phase SHA 不包含前序 Gate SHA")
            activation = self._require_phase_activation(
                record.requirement_id,
                record.phase,
                definition,
                require_current=True,
            )
            self._validate_receipt_bindings(
                record,
                definition,
                not_before=_require_timestamp(activation.activated_at, "activated_at"),
            )
        # 在全部指纹、Receipt 和前序链校验后再采样一次，
        # 缩小签发过程中 HEAD/dirty 状态的 TOCTOU 窗口。
        self._require_exact_head(record)

    def _require_exact_head(self, record: PhaseGateRecord) -> None:
        self._require_no_pending_reopen(record.requirement_id, record.phase)
        self._require_execution_context(record.requirement_id)
        current_head = self.git.head_sha()
        _require_sha(current_head, "current HEAD")
        if not self.git.is_clean():
            raise PhaseGateError("工作树存在未提交变更，不能签发 exact-SHA PASS Gate")
        if record.commit_sha != current_head:
            raise PhaseGateError(
                f"Phase Gate SHA 已陈旧：记录 {record.commit_sha}，当前 HEAD {current_head}"
            )

    def _require_execution_context(self, requirement_id: str) -> None:
        meta = self.workspace_store.load(requirement_id)["meta"]
        raw_git = meta.get("git")
        if not isinstance(raw_git, Mapping):
            return
        expected_worktree = raw_git.get("worktree")
        if isinstance(expected_worktree, str) and expected_worktree.strip():
            expected = os.path.normcase(os.path.abspath(expected_worktree))
            actual = os.path.normcase(
                os.path.abspath(str(self.workspace_store.working_root))
            )
            if expected != actual:
                raise PhaseGateError(
                    "当前执行目录不是 Requirement 绑定的 Git worktree"
                )
        expected_branch = raw_git.get("branch")
        current_branch = getattr(self.git, "current_branch", None)
        if (
            isinstance(expected_branch, str)
            and expected_branch.strip()
            and callable(current_branch)
            and current_branch() != expected_branch
        ):
            raise PhaseGateError("当前 Git branch 与 Requirement 绑定不一致")

    def _require_phase_activation(
        self,
        requirement_id: str,
        phase: int,
        definition: GateDefinition,
        *,
        require_current: bool,
    ) -> PhaseActivationRecord:
        activation = self.read_activation(requirement_id, phase)
        self._validate_activation(activation)
        if activation.task_id != definition.task_id:
            raise PhaseGateError("Activation Task 与本阶段 GateDefinition 不一致")
        if require_current:
            meta = self.workspace_store.load(requirement_id)["meta"]
            if meta.get("requirement_task_id") != definition.task_id:
                raise PhaseGateError("本阶段 Task 不是 Requirement 当前激活任务")
        return activation

    def _validate_receipt_bindings(
        self,
        record: PhaseGateRecord,
        definition: GateDefinition,
        *,
        not_before: datetime | None = None,
    ) -> None:
        receipt_ids = tuple(record.verification_receipt_refs)
        if len(set(receipt_ids)) != len(receipt_ids):
            raise PhaseGateError("Verification Receipt 引用不能重复")
        receipts = tuple(
            self.read_verification_receipt(record.requirement_id, receipt_id)
            for receipt_id in receipt_ids
        )
        issued_at = _require_timestamp(record.issued_at, "issued_at")
        reviewed_at = _require_timestamp(
            record.review_attestation.reviewed_at, "reviewed_at"
        )
        suites = {suite.suite_id: suite for suite in definition.verification_suites}
        observed_suites: set[str] = set()
        for receipt, expected_fingerprint in zip(
            receipts, record.verification_receipt_fingerprints, strict=True
        ):
            if verification_receipt_fingerprint(receipt) != expected_fingerprint:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 内容指纹已变化"
                )
            if receipt.commit_sha != record.commit_sha:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 未绑定 Gate exact SHA"
                )
            started_at = _require_timestamp(receipt.started_at, "started_at")
            completed_at = _require_timestamp(receipt.completed_at, "completed_at")
            if not_before is not None and started_at < not_before:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 早于本阶段 Activation"
                )
            if completed_at > reviewed_at:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 晚于独立 Review"
                )
            if completed_at > issued_at:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 晚于 Gate issued_at"
                )
            suite = suites.get(receipt.suite_id)
            if suite is None:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 引用了未声明 Suite"
                )
            if receipt.suite_fingerprint != suite.fingerprint:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} Suite 指纹不匹配"
                )
            if receipt.issuer != suite.expected_issuer:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 不是由受控执行器签发"
                )
            if receipt.command != suite.command_summary:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 未执行提交内声明的命令"
                )
            if suite.kind == "github-actions" and not receipt.source_url:
                raise PhaseGateError(
                    f"Verification Receipt {receipt.receipt_id} 缺少 GitHub run URL"
                )
            observed_suites.add(suite.suite_id)
        missing_suites = sorted(set(suites) - observed_suites)
        unexpected_suites = sorted(observed_suites - set(suites))
        if missing_suites or unexpected_suites:
            details: list[str] = []
            if missing_suites:
                details.append("缺失 " + ", ".join(missing_suites))
            if unexpected_suites:
                details.append("多出 " + ", ".join(unexpected_suites))
            raise PhaseGateError("Verification Suite 未精确覆盖：" + "；".join(details))
        declared = set(receipt_ids)
        missing_evidence = sorted(
            {
                evidence
                for result in record.acceptance_results
                for evidence in result.evidence_refs
                if evidence not in declared
            }
        )
        if missing_evidence:
            raise PhaseGateError(
                "AcceptanceResult 引用了未持久化证据：" + ", ".join(missing_evidence)
            )
        receipt_run_ids = {receipt.run_id for receipt in receipts}
        attested_run_ids = set(record.review_attestation.implementation_run_ids)
        if receipt_run_ids != attested_run_ids:
            missing_runs = sorted(attested_run_ids - receipt_run_ids)
            omitted_runs = sorted(receipt_run_ids - attested_run_ids)
            run_details: list[str] = []
            if missing_runs:
                run_details.append("未由 Receipt 证明 " + ", ".join(missing_runs))
            if omitted_runs:
                run_details.append("Review 漏报 " + ", ".join(omitted_runs))
            raise PhaseGateError(
                "实现 Run ID 与 Receipt 不完全一致：" + "；".join(run_details)
            )
        receipt_session_ids = {receipt.session_id for receipt in receipts}
        if record.review_attestation.reviewer_session_id in receipt_session_ids:
            raise PhaseGateError("Reviewer Session 参与了 Receipt 中的阶段实现")
        attested_session_ids = set(record.review_attestation.implementation_session_ids)
        if receipt_session_ids != attested_session_ids:
            missing_sessions = sorted(attested_session_ids - receipt_session_ids)
            omitted_sessions = sorted(receipt_session_ids - attested_session_ids)
            session_details: list[str] = []
            if missing_sessions:
                session_details.append("未由 Receipt 证明 " + ", ".join(missing_sessions))
            if omitted_sessions:
                session_details.append("Review 漏报 " + ", ".join(omitted_sessions))
            raise PhaseGateError(
                "实现 Session ID 与 Receipt 不完全一致："
                + "；".join(session_details)
            )

    def _validate_activation(self, activation: PhaseActivationRecord) -> None:
        if activation.schema_version != SCHEMA_VERSION:
            raise PhaseGateError(
                f"不支持 PhaseActivationRecord schema_version={activation.schema_version}"
            )
        if re.fullmatch(r"REQ-\d{3,}", activation.requirement_id) is None:
            raise PhaseGateError("PhaseActivationRecord Requirement ID 无效")
        if activation.phase < 1 or activation.predecessor_gate_phase != activation.phase - 1:
            raise PhaseGateError("Activation Record 必须引用直接前序 Gate")
        _require_sha(
            activation.predecessor_gate_commit_sha,
            "Activation predecessor_gate_commit_sha",
        )
        _require_fingerprint(
            activation.predecessor_gate_record_fingerprint,
            "Activation predecessor_gate_record_fingerprint",
        )
        activated_at = _require_timestamp(activation.activated_at, "activated_at")
        if activated_at > datetime.now(activated_at.tzinfo) + timedelta(minutes=5):
            raise PhaseGateError("Activation activated_at 不能是未来时间")
        if not activation.session_id.strip() or not activation.activated_by.strip():
            raise PhaseGateError("Activation session_id/activated_by 不能为空")
        predecessor = self.read(
            activation.requirement_id, activation.predecessor_gate_phase
        )
        self._validate_predecessor_chain(predecessor)
        issued_at = _require_timestamp(predecessor.issued_at, "predecessor issued_at")
        if activated_at < issued_at:
            raise PhaseGateError("Activation 不能早于前序 Gate 签发时间")
        definition = self.load_definition(
            activation.requirement_id,
            activation.predecessor_gate_phase,
            revision=predecessor.commit_sha,
        )
        if definition.next_task_id != activation.task_id:
            raise PhaseGateError("Activation Task 与前序 GateDefinition 不一致")
        if activation.predecessor_gate_commit_sha != predecessor.commit_sha:
            raise PhaseGateError("Activation 引用的 Gate SHA 已失效")
        if (
            activation.predecessor_gate_record_fingerprint
            != gate_record_fingerprint(predecessor)
        ):
            raise PhaseGateError("Activation 引用的 Gate Record 已失效")
        current_head = self.git.head_sha()
        _require_sha(current_head, "current HEAD")
        if current_head != predecessor.commit_sha and not self.git.is_ancestor(
            predecessor.commit_sha, current_head
        ):
            raise PhaseGateError("当前 HEAD 不包含 Activation 的前序 Gate SHA")

    def _validate_predecessor_chain(self, record: PhaseGateRecord) -> None:
        """验证持久化前序链未被替换，且每个前序 SHA 都被后继包含。"""

        self._require_no_pending_reopen(record.requirement_id, record.phase)
        _validate_pass_facts(record)
        self._require_frozen_definition(record)
        definitions = self.definitions(record.requirement_id, revision=record.commit_sha)
        if record.phase >= len(definitions):
            raise PhaseGateError(
                f"Phase {record.phase} 未在完整 GateDefinition 链中声明"
            )
        definition = definitions[record.phase]
        if (
            record.task_id != definition.task_id
            or record.plan_fingerprint != definition.plan_source_fingerprint
            or record.acceptance_fingerprint != definition.acceptance_fingerprint
            or {item.acceptance_id for item in record.acceptance_results}
            != set(definition.acceptance_ids)
        ):
            raise PhaseGateError(f"Phase {record.phase} 不匹配其提交中的 GateDefinition")
        if record.phase == 0:
            self._validate_receipt_bindings(record, definition)
            if any(
                value is not None
                for value in (
                    record.previous_gate_phase,
                    record.previous_gate_commit_sha,
                    record.previous_gate_record_fingerprint,
                )
            ):
                raise PhaseGateError("前序链中的 Phase 0 含非法引用")
            return
        previous = self.read(record.requirement_id, record.phase - 1)
        if previous.requirement_id != record.requirement_id:
            raise PhaseGateError(f"Phase {record.phase} 的前序 Gate 属于另一个 Requirement")
        if previous.phase != record.phase - 1:
            raise PhaseGateError(f"Phase {record.phase} 的前序 Gate phase 与路径不一致")
        self._validate_predecessor_chain(previous)
        if (
            record.previous_gate_phase != previous.phase
            or record.previous_gate_commit_sha != previous.commit_sha
            or record.previous_gate_record_fingerprint != gate_record_fingerprint(previous)
        ):
            raise PhaseGateError(f"Phase {record.phase} 的前序 Gate 引用已失效")
        if not self.git.is_ancestor(previous.commit_sha, record.commit_sha):
            raise PhaseGateError(f"Phase {record.phase} 的 SHA 不包含前序 Gate SHA")
        activation = self._require_phase_activation(
            record.requirement_id,
            record.phase,
            definition,
            require_current=False,
        )
        self._validate_receipt_bindings(
            record,
            definition,
            not_before=_require_timestamp(activation.activated_at, "activated_at"),
        )

    def _require_frozen_definition(self, record: PhaseGateRecord) -> None:
        """后续提交必须保留已签发定义的完整内容及其计划源。"""

        current_head = self.git.head_sha()
        path = self.definition_path(record.requirement_id, record.phase)
        try:
            issued = json.loads(self.git.read_file_at(record.commit_sha, path))
            current = json.loads(self.git.read_file_at(current_head, path))
        except (json.JSONDecodeError, GitError, OSError, RuntimeError) as exc:
            raise PhaseGateError(f"无法核对已签发 Phase {record.phase} 定义：{exc}") from exc
        # 比较原始 JSON 的规范指纹，同时保护未知字段；纯排版变化不算改写。
        if content_fingerprint(issued) != content_fingerprint(current):
            raise PhaseGateError(
                f"已签发 Phase {record.phase} GateDefinition 被当前提交改写"
            )
        self.load_definition(record.requirement_id, record.phase, revision=current_head)


class PhaseTransitionGuard:
    """Phase 2 前唯一允许把后序 Task 推进到 in_progress 的过渡门。"""

    def __init__(self, gates: GateStore, task_provider: TaskProvider) -> None:
        self.gates = gates
        self.task_provider = task_provider

    def advance_next(
        self,
        requirement_id: str,
        *,
        completed_phase: int,
        session_id: str,
        activated_by: str,
    ) -> Task:
        """
        以可重试顺序完成 Task/Session/meta/Activation 切换，最后才开放执行状态。
        """

        normalized = requirement_id.upper()
        if not session_id.strip() or not activated_by.strip():
            raise PhaseGateError("Phase 过渡必须绑定当前 Session 和 Agent")

        from .automation.session_runtime import (
            attach_session,
            rebind_session_task,
            session_task_ids,
        )

        with self.gates.workspace_store.provider_locked(normalized):
            activation_phase = completed_phase + 1
            activation_path = self.gates.activation_path_for(
                normalized, activation_phase
            )
            resuming_activation = activation_path.is_file()
            if resuming_activation:
                # The write-ahead Activation is the durable transition journal.
                # Validate it before looking at exact HEAD so stale retries can only
                # finish the already-authorized transition, never roll it back.
                activation = self.gates.read_activation(normalized, activation_phase)
                self.gates.validate_activation(activation)
                if activation.predecessor_gate_phase != completed_phase:
                    raise PhaseGateError("Activation 未引用请求的 completed phase")
                record = self.gates.read(normalized, completed_phase)
                definition = self.gates.load_definition(
                    normalized, completed_phase, revision=record.commit_sha
                )
                if (
                    activation.predecessor_gate_record_fingerprint
                    != gate_record_fingerprint(record)
                ):
                    raise PhaseGateError("Activation 与持久化 Gate 不一致")
                current_definitions = self.gates.definitions(normalized)
                if completed_phase >= len(current_definitions):
                    raise PhaseGateError(
                        f"Phase {completed_phase} 未在当前已提交 GateDefinition 链中声明"
                    )
                current_definition = current_definitions[completed_phase]
                if (
                    current_definition.task_id != definition.task_id
                    or current_definition.next_task_id != activation.task_id
                ):
                    raise PhaseGateError(
                        "Activation 与当前已提交 GateDefinition 链不一致"
                    )
            else:
                definitions = self.gates.definitions(normalized)
                if completed_phase >= len(definitions):
                    raise PhaseGateError(
                        f"Phase {completed_phase} 未在完整 GateDefinition 链中声明"
                    )
                definition = definitions[completed_phase]
                record = self.gates.read(normalized, completed_phase)
                self.gates.validate(record)
                next_task_id = definition.next_task_id
                if next_task_id is None:
                    raise PhaseGateError(f"Phase {completed_phase} 没有后序 Task")
                completed_task = self.task_provider.get_task(definition.task_id)
                next_task = self.task_provider.get_task(next_task_id)
                if completed_task.status not in {"in_progress", "in_review", "done"}:
                    raise PhaseGateError(
                        f"当前 Phase Task 状态不允许完成：{completed_task.status}"
                    )
                if next_task.status not in {"backlog", "todo", "blocked", "in_progress"}:
                    raise PhaseGateError(f"后序 Task 状态不允许推进：{next_task.status}")
                bound = session_task_ids(
                    self.gates.workspace_store, normalized, session_id
                )
                if definition.task_id not in bound and next_task_id not in bound:
                    raise PhaseGateError(
                        f"当前 Session 未绑定 Phase Task {definition.task_id}"
                    )
                activation = PhaseActivationRecord(
                    requirement_id=normalized,
                    phase=activation_phase,
                    task_id=next_task_id,
                    predecessor_gate_phase=completed_phase,
                    predecessor_gate_commit_sha=record.commit_sha,
                    predecessor_gate_record_fingerprint=gate_record_fingerprint(record),
                    session_id=session_id,
                    activated_at=now_iso(),
                    activated_by=activated_by,
                )
                # Persist authorization before any meta, Session, or Provider
                # side effect. A later process/session can deterministically resume.
                self.gates._write_activation(activation)

            next_task_id = definition.next_task_id
            if next_task_id is None or activation.task_id != next_task_id:
                raise PhaseGateError("Activation 与 GateDefinition 后序 Task 不一致")
            completed_task = self.task_provider.get_task(definition.task_id)
            next_task = self.task_provider.get_task(next_task_id)
            if completed_task.status not in {"in_progress", "in_review", "done"}:
                raise PhaseGateError(
                    f"当前 Phase Task 状态不允许完成：{completed_task.status}"
                )
            if next_task.status not in {"backlog", "todo", "blocked", "in_progress"}:
                raise PhaseGateError(f"后序 Task 状态不允许推进：{next_task.status}")
            transition_state = self.gates.workspace_store.load(normalized)
            current_task_id = transition_state["meta"].get("requirement_task_id")
            if current_task_id not in {definition.task_id, next_task_id}:
                raise PhaseGateError(
                    "Requirement 当前 Task 既不是已完成阶段也不是已授权后序阶段"
                )
            bound = session_task_ids(
                self.gates.workspace_store, normalized, session_id
            )
            activation_owner_active = any(
                item.get("id") == activation.session_id
                and item.get("result") == "in_progress"
                for item in transition_state["sessions"]
            )
            if activation.session_id != session_id and activation_owner_active:
                raise PhaseGateError(
                    f"Activation Session {activation.session_id} 仍活跃，"
                    f"Session {session_id} 不能接管阶段过渡"
                )
            if not bound and resuming_activation:
                # A crash can leave the journal/meta committed before Session rebind.
                # Once the journal owner is no longer active, the new caller may attach
                # only to the old/current authorized task and finish that same journal.
                attach_session(
                    self.gates.workspace_store,
                    normalized,
                    session_id=session_id,
                    agent_name=activated_by,
                    task_provider=self.task_provider,
                    task_ids=(current_task_id,),
                )
                bound = session_task_ids(
                    self.gates.workspace_store, normalized, session_id
                )
            if definition.task_id not in bound and next_task_id not in bound:
                raise PhaseGateError(
                    f"当前 Session 未绑定 Phase Task {definition.task_id}"
                )
            if current_task_id == definition.task_id:
                self.gates.workspace_store.compare_and_set_meta(
                    normalized,
                    field="requirement_task_id",
                    expected=definition.task_id,
                    value=next_task_id,
                )
            elif current_task_id != next_task_id:
                raise PhaseGateError(
                    "Requirement 当前 Task 既不是已完成阶段也不是已授权后序阶段"
                )
            rebind_session_task(
                self.gates.workspace_store,
                normalized,
                session_id=session_id,
                previous_task_id=definition.task_id,
                next_task_id=next_task_id,
                task_provider=self.task_provider,
            )
            if completed_task.status != "done":
                completed_task = self._compare_and_set_status(
                    completed_task, "done"
                )
            next_task = self.task_provider.get_task(next_task_id)
            if next_task.status != "in_progress":
                next_task = self._compare_and_set_status(next_task, "in_progress")
            return next_task

    def _compare_and_set_status(self, task: Task, status: str) -> Task:
        if task.version is None:
            raise PhaseGateError(f"Task {task.id} 缺少 version，阶段过渡不能执行 CAS")
        try:
            return self.task_provider.compare_and_set_status(
                task.id,
                expected_version=task.version,
                expected_status=task.status,
                status=status,
            )
        except TaskProviderError as exc:
            raise PhaseGateError(f"Task {task.id} 状态 CAS 失败：{exc}") from exc
