"""Phase 4 验证 Provider 合同与本地参考实现。

执行器只生成事实，不生成权威。只有仓库外受保护 policy 与 Ed25519 密钥控制的
attestor 才能把执行事实签发为权威 Receipt。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .integration.git_workspace import TrustedGit
from .orchestration.contracts import VerificationReceiptEnvelope, fingerprint

SuiteType = Literal["unit", "type", "lint", "integration", "e2e", "security", "custom"]
ExecutionMode = Literal["fail-fast", "collect-all"]
ResultStatus = Literal["PASS", "FAIL", "ERROR", "SKIPPED"]
_SUITE_TYPES = frozenset(("unit", "type", "lint", "integration", "e2e", "security", "custom"))
_FORBIDDEN_ENV = frozenset(("PATH", "PYTHONPATH", "PYTHONHOME"))


class VerificationProviderError(RuntimeError):
    """A stable fail-closed error surfaced by a verification boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: object) -> bytes:
    try:
        return rfc8785.dumps(cast(Any, value))
    except Exception as exc:
        raise VerificationProviderError("invalid_contract", "验证数据必须符合 RFC 8785/JCS") from exc


def _strict_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise VerificationProviderError("invalid_receipt", f"{label} 字段缺失或包含未知字段")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise VerificationProviderError("invalid_receipt", f"{label} 必须是非空字符串")
    return value


def _hex_digest(value: object, label: str, lengths: tuple[int, ...] = (64,)) -> str:
    text = _string(value, label)
    if len(text) not in lengths or re.fullmatch(r"[0-9a-f]+", text) is None:
        raise VerificationProviderError("invalid_receipt", f"{label} digest 无效")
    return text


def _digest_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _outside_repository(path: Path, repository: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        root = repository.resolve(strict=True)
    except OSError as exc:
        raise VerificationProviderError("authority_unavailable", f"{label} 不可用") from exc
    if resolved == root or root in resolved.parents:
        raise VerificationProviderError("untrusted_authority", f"{label} 必须位于 repository 外")
    if not resolved.is_file():
        raise VerificationProviderError("authority_unavailable", f"{label} 不可用")
    return resolved


@dataclass(frozen=True, slots=True)
class ArtifactConstraint:
    path: str
    required: bool = True
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        candidate = Path(self.path)
        if (not self.path or candidate.is_absolute() or ".." in candidate.parts
                or self.max_bytes is not None and self.max_bytes < 0):
            raise VerificationProviderError("invalid_artifact", "artifact 必须是安全相对路径")

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "required": self.required, "max_bytes": self.max_bytes}


@dataclass(frozen=True, slots=True)
class VerificationSuite:
    suite_id: str
    suite_type: SuiteType
    argv: tuple[str, ...]
    timeout_seconds: int = 300
    cwd: str = "."
    environment_allowlist: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    artifacts: tuple[ArtifactConstraint, ...] = ()
    requires_network: bool = False
    network_reader_id: str | None = None

    def __post_init__(self) -> None:
        cwd = Path(self.cwd)
        if not self.suite_id or self.suite_type not in _SUITE_TYPES or not self.argv:
            raise VerificationProviderError("invalid_suite", "suite identity/type/argv 无效")
        if self.timeout_seconds < 1 or cwd.is_absolute() or ".." in cwd.parts:
            raise VerificationProviderError("invalid_suite", "suite timeout/cwd 无效")
        if any(not isinstance(item, str) or "\0" in item for item in self.argv):
            raise VerificationProviderError("invalid_suite", "suite argv 必须是无 NUL 字符串")
        allowed = {item.upper() for item in self.environment_allowlist}
        supplied = {item.upper() for item in self.environment}
        if supplied - allowed:
            raise VerificationProviderError("environment_not_allowed", "suite 环境不在 allowlist")
        if supplied & _FORBIDDEN_ENV or allowed & _FORBIDDEN_ENV:
            raise VerificationProviderError("environment_injection", "禁止覆盖 PATH/PYTHONPATH")
        if self.requires_network and not self.network_reader_id:
            raise VerificationProviderError("network_unauthorized", "网络 suite 必须绑定 reader identity")

    def to_dict(self) -> dict[str, object]:
        return {
            "suite_id": self.suite_id, "suite_type": self.suite_type,
            "argv": list(self.argv), "timeout_seconds": self.timeout_seconds, "cwd": self.cwd,
            "environment_allowlist": list(self.environment_allowlist),
            "environment": dict(self.environment),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "requires_network": self.requires_network, "network_reader_id": self.network_reader_id,
        }


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    project_id: str
    requirement_id: str
    phase: int
    candidate_sha: str
    candidate_tree: str
    policy_fingerprint: str
    environment_digest: str
    suites: tuple[VerificationSuite, ...]
    required_suite_ids: tuple[str, ...]
    mode: ExecutionMode = "collect-all"

    def __post_init__(self) -> None:
        if not self.project_id or not self.requirement_id or self.phase < 0 or not self.suites:
            raise VerificationProviderError("invalid_plan", "plan identity/suites 无效")
        if self.mode not in ("fail-fast", "collect-all"):
            raise VerificationProviderError("invalid_plan", "未知执行策略")
        ids = tuple(item.suite_id for item in self.suites)
        if len(ids) != len(set(ids)):
            raise VerificationProviderError("invalid_plan", "suite id 重复")
        if not self.required_suite_ids or set(self.required_suite_ids) - set(ids):
            raise VerificationProviderError("missing_suite", "必需 suite 未在 plan 中声明")
        for value, name in ((self.candidate_sha, "SHA"), (self.candidate_tree, "tree"),
                            (self.policy_fingerprint, "policy"),
                            (self.environment_digest, "environment")):
            expected = (40, 64) if name != "policy" else (64,)
            if len(value) not in expected or any(char not in "0123456789abcdef" for char in value):
                raise VerificationProviderError("invalid_plan", f"{name} digest 无效")

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id, "requirement_id": self.requirement_id,
            "phase": self.phase, "candidate_sha": self.candidate_sha,
            "candidate_tree": self.candidate_tree, "policy_fingerprint": self.policy_fingerprint,
            "environment_digest": self.environment_digest,
            "suites": [item.to_dict() for item in self.suites],
            "required_suite_ids": list(self.required_suite_ids), "mode": self.mode,
        }

    @property
    def plan_fingerprint(self) -> str:
        return fingerprint(self.to_dict())


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    path: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ArtifactDigest:
        _strict_fields(value, {"path", "sha256", "size"}, "artifact")
        size = value["size"]
        if type(size) is not int or size < 0:
            raise VerificationProviderError("invalid_receipt", "artifact size 无效")
        path = _string(value["path"], "artifact path")
        ArtifactConstraint(path)
        return cls(path, _hex_digest(value["sha256"], "artifact sha256"), size)


@dataclass(frozen=True, slots=True)
class SuiteResult:
    suite_id: str
    suite_type: SuiteType
    status: ResultStatus
    returncode: int | None
    duration_seconds: float
    stdout_sha256: str
    stderr_sha256: str
    stdout_preview: str
    stderr_preview: str
    artifacts: tuple[ArtifactDigest, ...] = ()
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "suite_id": self.suite_id, "suite_type": self.suite_type, "status": self.status,
            "returncode": self.returncode, "duration_seconds": self.duration_seconds,
            "stdout_sha256": self.stdout_sha256, "stderr_sha256": self.stderr_sha256,
            "stdout_preview": self.stdout_preview, "stderr_preview": self.stderr_preview,
            "artifacts": [item.to_dict() for item in self.artifacts], "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SuiteResult:
        expected = {
            "suite_id", "suite_type", "status", "returncode", "duration_seconds",
            "stdout_sha256", "stderr_sha256", "stdout_preview", "stderr_preview",
            "artifacts", "error_code",
        }
        _strict_fields(value, expected, "suite result")
        suite_type = value["suite_type"]
        status = value["status"]
        returncode = value["returncode"]
        duration = value["duration_seconds"]
        artifacts = value["artifacts"]
        error = value["error_code"]
        if suite_type not in _SUITE_TYPES or status not in ("PASS", "FAIL", "ERROR", "SKIPPED"):
            raise VerificationProviderError("invalid_receipt", "suite type/status 无效")
        if returncode is not None and type(returncode) is not int:
            raise VerificationProviderError("invalid_receipt", "suite returncode 无效")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration < 0):
            raise VerificationProviderError("invalid_receipt", "suite duration 无效")
        if not isinstance(artifacts, list) or any(not isinstance(item, Mapping) for item in artifacts):
            raise VerificationProviderError("invalid_receipt", "suite artifacts 无效")
        if error is not None and not isinstance(error, str):
            raise VerificationProviderError("invalid_receipt", "suite error_code 无效")
        if ((status == "PASS" and (returncode != 0 or error is not None))
                or (status == "FAIL" and (returncode is None or returncode == 0))
                or (status in ("ERROR", "SKIPPED") and returncode is not None)
                or (status == "ERROR" and not error)):
            raise VerificationProviderError("invalid_receipt", "suite status/returncode 不一致")
        stdout_preview, stderr_preview = value["stdout_preview"], value["stderr_preview"]
        if not isinstance(stdout_preview, str) or not isinstance(stderr_preview, str):
            raise VerificationProviderError("invalid_receipt", "suite output preview 无效")
        return cls(
            _string(value["suite_id"], "suite_id"), cast(SuiteType, suite_type),
            status, returncode, float(duration),
            _hex_digest(value["stdout_sha256"], "stdout_sha256"),
            _hex_digest(value["stderr_sha256"], "stderr_sha256"),
            stdout_preview, stderr_preview,
            tuple(ArtifactDigest.from_dict(cast(Mapping[str, object], item)) for item in artifacts),
            error,
        )


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    schema_version: int
    receipt_id: str
    project_id: str
    requirement_id: str
    phase: int
    candidate_sha: str
    candidate_tree: str
    plan_fingerprint: str
    policy_fingerprint: str
    run_id: str
    attempt: int
    environment_digest: str
    started_at: str
    completed_at: str
    results: tuple[SuiteResult, ...]
    result: Literal["PASS", "FAIL"]
    artifact_digest: str
    provider_id: str
    provider_version: str
    legacy_receipt_fingerprint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "receipt_id": self.receipt_id,
            "project_id": self.project_id, "requirement_id": self.requirement_id,
            "phase": self.phase, "candidate_sha": self.candidate_sha,
            "candidate_tree": self.candidate_tree, "plan_fingerprint": self.plan_fingerprint,
            "policy_fingerprint": self.policy_fingerprint, "run_id": self.run_id,
            "attempt": self.attempt, "environment_digest": self.environment_digest,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "results": [item.to_dict() for item in self.results], "result": self.result,
            "artifact_digest": self.artifact_digest, "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "legacy_receipt_fingerprint": self.legacy_receipt_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> VerificationReceipt:
        expected = {
            "schema_version", "receipt_id", "project_id", "requirement_id", "phase",
            "candidate_sha", "candidate_tree", "plan_fingerprint", "policy_fingerprint",
            "run_id", "attempt", "environment_digest", "started_at", "completed_at",
            "results", "result", "artifact_digest", "provider_id", "provider_version",
            "legacy_receipt_fingerprint",
        }
        _strict_fields(value, expected, "receipt")
        if value["schema_version"] != 1 or type(value["schema_version"]) is not int:
            raise VerificationProviderError("unsupported_schema", "Receipt schema_version 不受支持")
        phase, attempt, results = value["phase"], value["attempt"], value["results"]
        if type(phase) is not int or phase < 0 or type(attempt) is not int or attempt < 1:
            raise VerificationProviderError("invalid_receipt", "Receipt phase/attempt 无效")
        if not isinstance(results, list) or not results or any(
            not isinstance(item, Mapping) for item in results
        ):
            raise VerificationProviderError("invalid_receipt", "Receipt results 无效")
        result = value["result"]
        if result not in ("PASS", "FAIL"):
            raise VerificationProviderError("invalid_receipt", "Receipt result 无效")
        legacy = value["legacy_receipt_fingerprint"]
        if legacy is not None:
            legacy = _hex_digest(legacy, "legacy_receipt_fingerprint")
        started, completed = _string(value["started_at"], "started_at"), _string(
            value["completed_at"], "completed_at",
        )
        try:
            start_time, end_time = datetime.fromisoformat(started), datetime.fromisoformat(completed)
            if start_time.tzinfo is None or end_time.tzinfo is None or end_time < start_time:
                raise ValueError
        except ValueError as exc:
            raise VerificationProviderError("invalid_receipt", "Receipt 时间无效") from exc
        return cls(
            1, _string(value["receipt_id"], "receipt_id"),
            _string(value["project_id"], "project_id"),
            _string(value["requirement_id"], "requirement_id"), phase,
            _hex_digest(value["candidate_sha"], "candidate_sha", (40, 64)),
            _hex_digest(value["candidate_tree"], "candidate_tree", (40, 64)),
            _hex_digest(value["plan_fingerprint"], "plan_fingerprint"),
            _hex_digest(value["policy_fingerprint"], "policy_fingerprint"),
            _string(value["run_id"], "run_id"), attempt,
            _hex_digest(value["environment_digest"], "environment_digest"), started, completed,
            tuple(SuiteResult.from_dict(cast(Mapping[str, object], item)) for item in results),
            result,
            _hex_digest(value["artifact_digest"], "artifact_digest"),
            _string(value["provider_id"], "provider_id"),
            _string(value["provider_version"], "provider_version"), legacy,
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessRunner(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
                 timeout: int) -> ProcessResult: ...


class VerificationPlannerProvider(Protocol):
    """把受保护 policy、候选身份与 suite 定义冻结为可审计计划。"""

    def plan(
        self, *, project_id: str, requirement_id: str, phase: int,
        candidate_sha: str, candidate_tree: str, environment: Mapping[str, str],
        suites: tuple[VerificationSuite, ...], required_suite_ids: tuple[str, ...],
        mode: ExecutionMode = "collect-all",
    ) -> VerificationPlan: ...


class VerificationExecutorProvider(Protocol):
    """执行已冻结计划；返回值仍需受保护 attestor 签名。"""

    def execute(
        self, plan: VerificationPlan, *, workspace: Path, run_id: str | None = None,
        attempt: int = 1,
    ) -> VerificationReceipt: ...


@dataclass(frozen=True, slots=True)
class RuleVerificationPlannerProvider:
    """确定性本地 Planner；policy fingerprint 必须来自仓库外受保护 policy。"""

    policy_fingerprint: str

    def plan(
        self, *, project_id: str, requirement_id: str, phase: int,
        candidate_sha: str, candidate_tree: str, environment: Mapping[str, str],
        suites: tuple[VerificationSuite, ...], required_suite_ids: tuple[str, ...],
        mode: ExecutionMode = "collect-all",
    ) -> VerificationPlan:
        return VerificationPlan(
            project_id, requirement_id, phase, candidate_sha, candidate_tree,
            self.policy_fingerprint, fingerprint(dict(environment)), suites,
            required_suite_ids, mode,
        )


def _subprocess_runner(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
                       timeout: int) -> ProcessResult:
    completed = subprocess.run(
        list(argv), cwd=cwd, env=dict(env), shell=False, capture_output=True,
        timeout=timeout, check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


class LocalVerificationProvider:
    provider_id = "local-verification-provider"
    provider_version = "1"

    def __init__(self, *, environment: Mapping[str, str], runner: ProcessRunner = _subprocess_runner,
                 output_limit: int = 4096,
                 candidate_identity: Callable[[Path], tuple[str, str]] | None = None) -> None:
        if output_limit < 0:
            raise VerificationProviderError("invalid_provider", "输出上限不能为负数")
        self._environment = dict(environment)
        self._runner = runner
        self._output_limit = output_limit
        self._candidate_identity = candidate_identity

    def execute(self, plan: VerificationPlan, *, workspace: Path, run_id: str | None = None,
                attempt: int = 1) -> VerificationReceipt:
        root = workspace.resolve(strict=True)
        if fingerprint(self._environment) != plan.environment_digest:
            raise VerificationProviderError("environment_mismatch", "执行环境与 plan 不匹配")
        self._assert_candidate(plan, root)
        started_at = datetime.now(UTC).isoformat()
        results: list[SuiteResult] = []
        for suite in plan.suites:
            if plan.mode == "fail-fast" and any(item.status != "PASS" for item in results):
                results.append(self._skipped(suite))
                continue
            results.append(self._run_suite(suite, root))
        status_by_id = {item.suite_id: item.status for item in results}
        passed = all(status_by_id.get(item) == "PASS" for item in plan.required_suite_ids)
        artifacts = [artifact.to_dict() for result in results for artifact in result.artifacts]
        self._assert_candidate(plan, root)
        return VerificationReceipt(
            1, f"receipt-{uuid4().hex}", plan.project_id, plan.requirement_id, plan.phase,
            plan.candidate_sha, plan.candidate_tree, plan.plan_fingerprint,
            plan.policy_fingerprint, run_id or f"run-{uuid4().hex}", attempt,
            fingerprint(self._environment), started_at, datetime.now(UTC).isoformat(),
            tuple(results), "PASS" if passed else "FAIL", fingerprint(artifacts),
            self.provider_id, self.provider_version,
        )

    def _assert_candidate(self, plan: VerificationPlan, root: Path) -> None:
        if self._candidate_identity is None:
            try:
                git = TrustedGit(root)
                actual = (
                    git.resolve("HEAD", cwd=root),
                    git.run("rev-parse", "HEAD^{tree}", cwd=root),
                )
            except Exception as exc:
                raise VerificationProviderError(
                    "candidate_unavailable", "无法从可信 Git 读取 candidate identity",
                ) from exc
        else:
            actual = self._candidate_identity(root)
        if actual != (plan.candidate_sha, plan.candidate_tree):
            raise VerificationProviderError("stale_verification", "workspace candidate 已变化")

    def _run_suite(self, suite: VerificationSuite, root: Path) -> SuiteResult:
        cwd = (root / suite.cwd).resolve()
        if cwd != root and root not in cwd.parents:
            raise VerificationProviderError("invalid_cwd", "suite cwd 越出 workspace")
        env = {name: self._environment[name] for name in suite.environment_allowlist
               if name in self._environment}
        env.update(suite.environment)
        started = time.monotonic()
        try:
            completed = self._runner(suite.argv, cwd=cwd, env=env, timeout=suite.timeout_seconds)
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
            status: ResultStatus = "PASS"
            error = None
            if code != 0:
                status = "FAIL"
        except subprocess.TimeoutExpired as exc:
            code, status, error = None, "ERROR", "timeout"
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
        except OSError as exc:
            code, status, error = None, "ERROR", "process_unavailable"
            stdout, stderr = b"", str(exc).encode("utf-8", errors="replace")
        artifacts: list[ArtifactDigest] = []
        if status == "PASS":
            try:
                artifacts = self._collect_artifacts(root, suite.artifacts)
            except VerificationProviderError as exc:
                status, code, error = "ERROR", None, exc.code
        return SuiteResult(
            suite.suite_id, suite.suite_type, status, code, time.monotonic() - started,
            hashlib.sha256(stdout).hexdigest(), hashlib.sha256(stderr).hexdigest(),
            stdout[:self._output_limit].decode("utf-8", errors="replace"),
            stderr[:self._output_limit].decode("utf-8", errors="replace"), tuple(artifacts), error,
        )

    @staticmethod
    def _collect_artifacts(root: Path, constraints: tuple[ArtifactConstraint, ...]) -> list[ArtifactDigest]:
        result: list[ArtifactDigest] = []
        for constraint in constraints:
            path = (root / constraint.path).resolve()
            if path != root and root not in path.parents:
                raise VerificationProviderError("invalid_artifact", "artifact 越出 workspace")
            if not path.is_file():
                if constraint.required:
                    raise VerificationProviderError("artifact_missing", "必需 artifact 缺失")
                continue
            size = path.stat().st_size
            if constraint.max_bytes is not None and size > constraint.max_bytes:
                raise VerificationProviderError("artifact_too_large", "artifact 超出约束")
            result.append(ArtifactDigest(constraint.path, _digest_file(path), size))
        return result

    @staticmethod
    def _skipped(suite: VerificationSuite) -> SuiteResult:
        empty = hashlib.sha256(b"").hexdigest()
        return SuiteResult(suite.suite_id, suite.suite_type, "SKIPPED", None, 0, empty, empty,
                           "", "", error_code="fail_fast")


class FakeVerificationProvider(LocalVerificationProvider):
    """Deterministic contract-test provider; never represents authoritative execution."""

    provider_id = "fake-verification-provider"

    def _assert_candidate(self, plan: VerificationPlan, root: Path) -> None:
        if (self._candidate_identity is not None
                and self._candidate_identity(root) != (plan.candidate_sha, plan.candidate_tree)):
            raise VerificationProviderError("stale_verification", "fixture candidate 已变化")

    def __init__(self, *, environment: Mapping[str, str], runner: ProcessRunner,
                 output_limit: int = 4096,
                 candidate_identity: Callable[[Path], tuple[str, str]] | None = None) -> None:
        super().__init__(
            environment=environment, runner=runner, output_limit=output_limit,
            candidate_identity=candidate_identity,
        )


@dataclass(frozen=True, slots=True)
class AttestorPolicy:
    policy_id: str
    policy_fingerprint: str
    allowed_key_ids: tuple[str, ...]
    allowed_suite_types: tuple[SuiteType, ...]
    network_reader_ids: tuple[str, ...] = ()
    allow_legacy_migration: bool = False

    @classmethod
    def load(cls, path: Path, *, repository: Path) -> AttestorPolicy:
        trusted = _outside_repository(path, repository, "attestor policy")
        try:
            payload = json.loads(trusted.read_text(encoding="utf-8"))
            claimed = payload.pop("policy_fingerprint")
            actual = fingerprint(payload)
            if claimed != actual:
                raise VerificationProviderError("policy_downgrade", "attestor policy 指纹不匹配")
            return cls(
                payload["policy_id"], claimed, tuple(payload["allowed_key_ids"]),
                tuple(payload["allowed_suite_types"]), tuple(payload.get("network_reader_ids", ())),
                bool(payload.get("allow_legacy_migration", False)),
            )
        except VerificationProviderError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise VerificationProviderError("authority_unavailable", "无法加载 attestor policy") from exc

    def authorize(self, plan: VerificationPlan, key_id: str) -> None:
        if plan.policy_fingerprint != self.policy_fingerprint:
            raise VerificationProviderError("policy_downgrade", "plan 未绑定受保护 policy")
        if key_id not in self.allowed_key_ids:
            raise VerificationProviderError("untrusted_key", "签名 key 未获 policy 授权")
        for suite in plan.suites:
            if suite.suite_type not in self.allowed_suite_types:
                raise VerificationProviderError("suite_not_allowed", "suite type 未获授权")
            if suite.requires_network and suite.network_reader_id not in self.network_reader_ids:
                raise VerificationProviderError("network_unauthorized", "网络 reader 未获授权")


@dataclass(frozen=True, slots=True)
class SignedReceiptEnvelope:
    schema_version: int
    canonicalization: str
    algorithm: str
    key_id: str
    attestor_id: str
    payload: Mapping[str, object]
    signature: str

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "canonicalization": self.canonicalization,
                "algorithm": self.algorithm,
                "key_id": self.key_id, "attestor_id": self.attestor_id,
                "payload": dict(self.payload), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SignedReceiptEnvelope:
        fields = {"schema_version", "canonicalization", "algorithm", "key_id", "attestor_id",
                  "payload", "signature"}
        if set(value) != fields or not isinstance(value.get("payload"), Mapping):
            raise VerificationProviderError("unknown_header", "签名 envelope 字段不受支持")
        schema_version = value["schema_version"]
        if type(schema_version) is not int:
            raise VerificationProviderError("invalid_envelope", "签名 envelope schema 无效")
        try:
            return cls(
                schema_version, str(value["canonicalization"]),
                str(value["algorithm"]), str(value["key_id"]), str(value["attestor_id"]),
                cast(Mapping[str, object], value["payload"]), str(value["signature"]),
            )
        except (TypeError, ValueError) as exc:
            raise VerificationProviderError("invalid_envelope", "签名 envelope 无效") from exc

    def protected(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "canonicalization": self.canonicalization,
            "algorithm": self.algorithm, "key_id": self.key_id, "attestor_id": self.attestor_id,
        }


@dataclass(frozen=True, slots=True)
class ProtectedExecutionContext:
    candidate_sha: str
    candidate_tree: str
    environment_digest: str
    network_reader_ids: tuple[str, ...] = ()


class AttestationContextReader(Protocol):
    def __call__(self, plan: VerificationPlan) -> ProtectedExecutionContext: ...


class ReceiptAttestor:
    def __init__(self, *, repository: Path, policy_path: Path, private_key_path: Path,
                 key_id: str, attestor_id: str,
                 context_reader: AttestationContextReader) -> None:
        self.policy = AttestorPolicy.load(policy_path, repository=repository)
        key_path = _outside_repository(private_key_path, repository, "private key")
        try:
            loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise VerificationProviderError("authority_unavailable", "无法加载签名密钥") from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise VerificationProviderError("invalid_key", "签名密钥必须是 Ed25519")
        if key_id not in self.policy.allowed_key_ids:
            raise VerificationProviderError("untrusted_key", "签名 key 未获 policy 授权")
        self._key, self.key_id, self.attestor_id = loaded, key_id, attestor_id
        self._context_reader = context_reader

    def sign(self, receipt: VerificationReceipt, plan: VerificationPlan) -> SignedReceiptEnvelope:
        self.policy.authorize(plan, self.key_id)
        try:
            rebuilt = self._context_reader(plan)
        except Exception as exc:
            raise VerificationProviderError("attestor_offline", "attestor 无法重建执行上下文") from exc
        required_readers = {suite.network_reader_id for suite in plan.suites if suite.requires_network}
        if (rebuilt.candidate_sha, rebuilt.candidate_tree, rebuilt.environment_digest) != (
            plan.candidate_sha, plan.candidate_tree, plan.environment_digest,
        ):
            raise VerificationProviderError("stale_verification", "attestor 重建上下文不匹配")
        if None in required_readers or not required_readers.issubset(rebuilt.network_reader_ids):
            raise VerificationProviderError("network_unauthorized", "attestor 未核验 network reader")
        context = (
            receipt.project_id, receipt.requirement_id, receipt.phase, receipt.candidate_sha,
            receipt.candidate_tree, receipt.plan_fingerprint, receipt.policy_fingerprint,
            receipt.environment_digest,
        )
        expected = (
            plan.project_id, plan.requirement_id, plan.phase, plan.candidate_sha,
            plan.candidate_tree, plan.plan_fingerprint, self.policy.policy_fingerprint,
            plan.environment_digest,
        )
        if context != expected:
            raise VerificationProviderError("stale_verification", "Receipt 与 plan/policy 不匹配")
        if receipt.legacy_receipt_fingerprint is not None and not self.policy.allow_legacy_migration:
            raise VerificationProviderError("legacy_migration_forbidden", "policy 禁止 legacy migration")
        payload = receipt.to_dict()
        envelope = SignedReceiptEnvelope(
            1, "RFC8785", "Ed25519", self.key_id, self.attestor_id, payload, "",
        )
        signed = {"protected": envelope.protected(), "payload": payload}
        signature = base64.b64encode(self._key.sign(_canonical(signed))).decode("ascii")
        return replace(envelope, signature=signature)


@dataclass(frozen=True, slots=True)
class TrustStore:
    trust_store_id: str
    keys: Mapping[str, Ed25519PublicKey]
    attestor_keys: Mapping[str, tuple[str, ...]]

    @classmethod
    def load(cls, path: Path, *, repository: Path) -> TrustStore:
        trusted = _outside_repository(path, repository, "trust store")
        try:
            payload = json.loads(trusted.read_text(encoding="utf-8"))
            keys = {
                key_id: Ed25519PublicKey.from_public_bytes(base64.b64decode(value, validate=True))
                for key_id, value in payload["keys"].items()
            }
            attestor_keys = {
                attestor: tuple(key_ids) for attestor, key_ids in payload["attestors"].items()
            }
            return cls(payload["trust_store_id"], keys, attestor_keys)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise VerificationProviderError("authority_unavailable", "无法加载 trust store") from exc

    def verify(self, envelope: SignedReceiptEnvelope, *, plan: VerificationPlan,
               policy: AttestorPolicy, expected_run_id: str,
               expected_attempt: int) -> Mapping[str, object]:
        if (envelope.schema_version != 1 or envelope.canonicalization != "RFC8785"
                or envelope.algorithm != "Ed25519"):
            raise VerificationProviderError("unknown_header", "签名 envelope header 不受支持")
        if not envelope.signature:
            raise VerificationProviderError("unsigned_receipt", "Receipt 缺少可信 Ed25519 签名")
        if (envelope.key_id not in self.keys
                or envelope.key_id not in self.attestor_keys.get(envelope.attestor_id, ())):
            raise VerificationProviderError("untrusted_attestor", "Receipt attestor/key 不受信")
        policy.authorize(plan, envelope.key_id)
        try:
            signature = base64.b64decode(envelope.signature, validate=True)
            signed = {"protected": envelope.protected(), "payload": envelope.payload}
            self.keys[envelope.key_id].verify(signature, _canonical(signed))
        except (ValueError, InvalidSignature) as exc:
            raise VerificationProviderError("invalid_signature", "Receipt 签名无效") from exc
        receipt = VerificationReceipt.from_dict(envelope.payload)
        payload = receipt.to_dict()
        expected: dict[str, object] = {
            "project_id": plan.project_id, "requirement_id": plan.requirement_id,
            "phase": plan.phase, "candidate_sha": plan.candidate_sha,
            "candidate_tree": plan.candidate_tree, "plan_fingerprint": plan.plan_fingerprint,
            "policy_fingerprint": policy.policy_fingerprint, "run_id": expected_run_id,
            "attempt": expected_attempt, "environment_digest": plan.environment_digest,
        }
        if any(payload.get(name) != value for name, value in expected.items()):
            raise VerificationProviderError("context_replay", "Receipt 上下文与当前执行不匹配")
        if tuple(item.suite_id for item in receipt.results) != tuple(
            suite.suite_id for suite in plan.suites
        ) or any(
            result.suite_type != suite.suite_type
            for result, suite in zip(receipt.results, plan.suites, strict=True)
        ):
            raise VerificationProviderError("invalid_receipt", "Receipt suite 结构与 plan 不一致")
        results = payload.get("results")
        if not isinstance(results, list):
            raise VerificationProviderError("invalid_receipt", "Receipt results 无效")
        status_by_id = {item.get("suite_id"): item.get("status") for item in results
                        if isinstance(item, dict)}
        if (payload.get("result") != "PASS"
                or any(status_by_id.get(item) != "PASS" for item in plan.required_suite_ids)):
            raise VerificationProviderError("verification_failed", "必需 suite 未全部 PASS")
        expected_artifact = fingerprint([
            artifact for result in results if isinstance(result, dict)
            for artifact in result.get("artifacts", []) if isinstance(artifact, dict)
        ])
        if payload.get("artifact_digest") != expected_artifact:
            raise VerificationProviderError("artifact_changed", "Receipt artifact digest 不匹配")
        return payload


def migrate_legacy_receipt(legacy: VerificationReceiptEnvelope, *, plan: VerificationPlan,
                           policy: AttestorPolicy, suite_types: Mapping[str, SuiteType],
                           run_id: str | None = None,
                           attempt: int = 1) -> VerificationReceipt:
    """Explicit, non-authoritative migration; an attestor must still authorize and sign it."""

    if not policy.allow_legacy_migration or policy.policy_fingerprint != plan.policy_fingerprint:
        raise VerificationProviderError("legacy_migration_forbidden", "policy 禁止 legacy migration")
    if set(suite_types) != {item.command_id for item in legacy.results}:
        raise VerificationProviderError("legacy_migration_incomplete", "legacy suite 映射不完整")
    if (legacy.requirement_id != plan.requirement_id
            or legacy.candidate_sha != plan.candidate_sha
            or legacy.candidate_tree != plan.candidate_tree
            or tuple(item.command_id for item in legacy.results)
            != tuple(item.suite_id for item in plan.suites)):
        raise VerificationProviderError("stale_verification", "legacy Receipt 与目标 plan 不匹配")
    results = tuple(
        SuiteResult(
            item.command_id, suite_types[item.command_id], "PASS" if item.returncode == 0 else "FAIL",
            item.returncode, float(item.duration_seconds), item.stdout_sha256, item.stderr_sha256,
            "", "",
        )
        for item in legacy.results
    )
    legacy_fingerprint = fingerprint(legacy.to_dict())
    return VerificationReceipt(
        1, f"migrated-{legacy.receipt_id}", plan.project_id, legacy.requirement_id, plan.phase,
        legacy.candidate_sha, legacy.candidate_tree, plan.plan_fingerprint,
        plan.policy_fingerprint, run_id or legacy.receipt_id, attempt,
        plan.environment_digest, legacy.started_at, legacy.completed_at, results,
        "PASS" if all(item.status == "PASS" for item in results) else "FAIL",
        fingerprint([]), "legacy-verification-migration", "1", legacy_fingerprint,
    )
