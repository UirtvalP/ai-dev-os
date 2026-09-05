"""绑定 exact Git SHA 的版本化阶段门禁记录。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Self

from .adapters.base import TaskProvider
from .adapters.git import GitError, LocalGitProvider
from .models import Task
from .workspace import WorkspaceError, WorkspaceStore

SCHEMA_VERSION = 1
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")


class PhaseGateError(WorkspaceError):
    """阶段门禁记录缺失、损坏或不再匹配当前事实。"""


class GitRevisionReader(Protocol):
    """阶段门禁所需的最小只读 Git 能力。"""

    def head_sha(self) -> str: ...

    def is_clean(self) -> bool: ...

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool: ...

    def read_file_at(self, revision: str, relative_path: str) -> str: ...


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
                        "regression_summary",
                        "review_attestation",
                        "issued_at",
                        "issued_by",
                        "status",
                    }
                ),
            ),
        )


def gate_record_fingerprint(record: PhaseGateRecord) -> str:
    """生成前序 Gate 引用使用的内容指纹。"""

    return content_fingerprint(record.to_dict())


def _require_sha(value: str, label: str) -> None:
    if _SHA_PATTERN.fullmatch(value) is None:
        raise PhaseGateError(f"{label} 必须是 40 位小写十六进制 Git SHA")


def _require_fingerprint(value: str, label: str) -> None:
    if _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise PhaseGateError(f"{label} 必须是 64 位小写十六进制 SHA-256")


def _require_timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PhaseGateError(f"{label} 必须是有效 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PhaseGateError(f"{label} 必须包含时区")


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
    _require_timestamp(record.issued_at, "issued_at")
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
    if not record.verification_receipt_refs:
        raise PhaseGateError("PASS Gate 必须引用至少一个 Verification Receipt")
    if any(not item.strip() for item in record.verification_receipt_refs):
        raise PhaseGateError("Verification Receipt 引用不能为空")
    review = record.review_attestation
    if review.schema_version != SCHEMA_VERSION:
        raise PhaseGateError("不支持 ReviewAttestation schema_version")
    if review.verdict != "PASS":
        raise PhaseGateError(f"独立 Review 未通过：{review.verdict}")
    if not review.reviewer_session_id.strip():
        raise PhaseGateError("Reviewer Session 不能为空")
    if not review.implementation_session_ids:
        raise PhaseGateError("至少需要一个实现 Session")
    if review.reviewer_session_id in review.implementation_session_ids:
        raise PhaseGateError("Reviewer Session 不能参与本阶段实现")
    if len(set(review.implementation_session_ids)) != len(review.implementation_session_ids):
        raise PhaseGateError("实现 Session ID 不能重复")
    if not review.implementation_run_ids:
        raise PhaseGateError("至少需要一个实现 Run ID")
    if len(set(review.implementation_run_ids)) != len(review.implementation_run_ids):
        raise PhaseGateError("实现 Run ID 不能重复")
    _require_timestamp(review.reviewed_at, "reviewed_at")


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

    def read(self, requirement_id: str, phase: int) -> PhaseGateRecord:
        path = self.path_for(requirement_id, phase)
        if not path.is_file():
            raise PhaseGateError(f"缺少前序 Phase Gate：{path}")
        payload = self.workspace_store.read_json(path)
        return PhaseGateRecord.from_dict(_object_mapping(payload, str(path)))

    def write(
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

    def issue(
        self,
        *,
        requirement_id: str,
        phase: int,
        acceptance_results: tuple[AcceptanceResult, ...],
        verification_receipt_refs: tuple[str, ...],
        regression_summary: str,
        review_attestation: ReviewAttestation,
        issued_at: str,
        issued_by: str,
    ) -> PhaseGateRecord:
        """从当前 HEAD 和持久化前序 Gate 确定性生成并写入 PASS 记录。"""

        definition = self.load_definition(requirement_id, phase)
        previous = self.read(requirement_id, phase - 1) if phase > 0 else None
        record = PhaseGateRecord(
            requirement_id=requirement_id.upper(),
            phase=phase,
            task_id=definition.task_id,
            commit_sha=self.git.head_sha(),
            previous_gate_phase=previous.phase if previous else None,
            previous_gate_commit_sha=previous.commit_sha if previous else None,
            previous_gate_record_fingerprint=(
                gate_record_fingerprint(previous) if previous else None
            ),
            plan_fingerprint=definition.plan_source_fingerprint,
            acceptance_fingerprint=definition.acceptance_fingerprint,
            acceptance_results=acceptance_results,
            verification_receipt_refs=verification_receipt_refs,
            regression_summary=regression_summary,
            review_attestation=review_attestation,
            issued_at=issued_at,
            issued_by=issued_by,
            status="PASS",
        )
        self.write(record)
        return record

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
        current_head = self.git.head_sha()
        _require_sha(current_head, "current HEAD")
        if not self.git.is_clean():
            raise PhaseGateError("工作树存在未提交变更，不能签发 exact-SHA PASS Gate")
        if record.commit_sha != current_head:
            raise PhaseGateError(
                f"Phase Gate SHA 已陈旧：记录 {record.commit_sha}，当前 HEAD {current_head}"
            )
        definition = self.load_definition(record.requirement_id, record.phase)
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
            return
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
            raise PhaseGateError("previous_gate_record_fingerprint 与前序 Gate 不一致")
        if not self.git.is_ancestor(previous.commit_sha, record.commit_sha):
            raise PhaseGateError("当前 Phase SHA 不包含前序 Gate SHA")

    def _validate_predecessor_chain(self, record: PhaseGateRecord) -> None:
        """验证持久化前序链未被替换，且每个前序 SHA 都被后继包含。"""

        _validate_pass_facts(record)
        definition = self.load_definition(
            record.requirement_id, record.phase, revision=record.commit_sha
        )
        if (
            record.task_id != definition.task_id
            or record.plan_fingerprint != definition.plan_source_fingerprint
            or record.acceptance_fingerprint != definition.acceptance_fingerprint
            or {item.acceptance_id for item in record.acceptance_results}
            != set(definition.acceptance_ids)
        ):
            raise PhaseGateError(f"Phase {record.phase} 不匹配其提交中的 GateDefinition")
        if record.phase == 0:
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
        next_task_id: str,
    ) -> Task:
        """验证 exact-SHA Gate 后 CAS 推进；无效的手工 in_progress 会收敛为 blocked。"""

        task = self.task_provider.get_task(next_task_id)
        try:
            record = self.gates.read(requirement_id, completed_phase)
            self.gates.validate(record)
            definition = self.gates.load_definition(requirement_id, completed_phase)
            if definition.next_task_id != next_task_id:
                raise PhaseGateError("后序 Task 与 GateDefinition 不一致")
        except PhaseGateError:
            if task.status == "in_progress":
                self.task_provider.update_status(task.id, "blocked")
            raise
        if task.status == "in_progress":
            return task
        if task.status not in {"backlog", "todo", "blocked"}:
            raise PhaseGateError(f"后序 Task 状态不允许推进：{task.status}")
        return self.task_provider.update_status(task.id, "in_progress")
