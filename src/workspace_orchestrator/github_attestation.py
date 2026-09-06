"""GitHub Actions OIDC / Artifact Attestation 的受保护 Receipt 验证边界。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .orchestration.contracts import fingerprint
from .verification_provider import (
    ArtifactConstraint,
    VerificationPlan,
    VerificationProviderError,
    VerificationReceipt,
    VerificationSuite,
    _canonical,
)

_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_PROVENANCE_PREDICATE = "https://slsa.dev/provenance/v1"
_ATTEST_ACTION_SHA = "977bb373ede98d70efdf65b84cb5f73e068dcc2a"
_SHA1 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[1-9][0-9]*")


class JsonReader(Protocol):
    def __call__(self, url: str) -> Mapping[str, object]: ...


class CommandRunner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[str]: ...


def _read_github_json(url: str) -> Mapping[str, object]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-dev-os-github-attestation",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, Mapping):
        raise VerificationProviderError("attestor_offline", "GitHub API 返回了无效 JSON")
    return cast(Mapping[str, object], payload)


def _run_command(
    argv: Sequence[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, check=False, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, shell=False,
    )


def _strict(value: Mapping[str, object], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise VerificationProviderError("invalid_attestation", f"{label} 字段无效")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationProviderError("invalid_attestation", f"{label} 必须是非空字符串")
    return value


def _sha(value: object, label: str, *, sha256: bool = False) -> str:
    text = _text(value, label)
    if ( _SHA256 if sha256 else _SHA1).fullmatch(text) is None:
        raise VerificationProviderError("invalid_attestation", f"{label} digest 无效")
    return text


@dataclass(frozen=True, slots=True)
class GitHubAttestationTrustPolicy:
    """仓库外只读 policy；它固定身份和代码，不携带 signing secret。"""

    repository: str
    workflow_path: str
    workflow_ref: str
    workflow_sha256: str
    runner_path: str
    runner_sha256: str
    attestor_policy_path: str
    attestor_policy_fingerprint: str
    action_sha: str
    gh_path: Path
    gh_sha256: str

    @classmethod
    def load(cls, path: Path, *, repository: Path) -> GitHubAttestationTrustPolicy:
        try:
            trusted = path.resolve(strict=True)
            root = repository.resolve(strict=True)
            if trusted == root or root in trusted.parents or not trusted.is_file():
                raise VerificationProviderError(
                    "untrusted_authority", "GitHub attestation policy 必须位于 repository 外",
                )
            payload = json.loads(trusted.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise TypeError
            _strict(
                payload,
                {
                    "schema_version", "repository", "workflow_path", "workflow_ref",
                    "workflow_sha256", "runner_path", "runner_sha256",
                    "attestor_policy_path", "attestor_policy_fingerprint", "action_sha",
                    "gh_path", "gh_sha256",
                },
                "GitHub attestation policy",
            )
            if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
                raise VerificationProviderError("unsupported_schema", "GitHub policy schema 不受支持")
            result = cls(
                repository=_text(payload["repository"], "repository"),
                workflow_path=_text(payload["workflow_path"], "workflow_path"),
                workflow_ref=_text(payload["workflow_ref"], "workflow_ref"),
                workflow_sha256=_sha(payload["workflow_sha256"], "workflow_sha256", sha256=True),
                runner_path=_text(payload["runner_path"], "runner_path"),
                runner_sha256=_sha(payload["runner_sha256"], "runner_sha256", sha256=True),
                attestor_policy_path=_text(payload["attestor_policy_path"], "policy path"),
                attestor_policy_fingerprint=_sha(
                    payload["attestor_policy_fingerprint"], "policy fingerprint", sha256=True,
                ),
                action_sha=_sha(payload["action_sha"], "action SHA"),
                gh_path=Path(_text(payload["gh_path"], "gh_path")),
                gh_sha256=_sha(payload["gh_sha256"], "gh_sha256", sha256=True),
            )
            result.validate(repository=root)
            return result
        except VerificationProviderError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise VerificationProviderError(
                "authority_unavailable", "无法加载 GitHub attestation policy",
            ) from exc

    def validate(self, *, repository: Path) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise VerificationProviderError("invalid_attestation", "GitHub repository 身份无效")
        safe_paths = (self.workflow_path, self.runner_path, self.attestor_policy_path)
        if (
            self.workflow_ref != "refs/heads/main"
            or self.action_sha != _ATTEST_ACTION_SHA
            or not self.workflow_path.startswith(".github/workflows/")
            or any(Path(value).is_absolute() or ".." in Path(value).parts for value in safe_paths)
        ):
            raise VerificationProviderError(
                "untrusted_attestor", "GitHub attestor 必须固定 main 与安全 workflow/runner/policy 路径",
            )
        if not self.gh_path.is_absolute():
            raise VerificationProviderError(
                "untrusted_attestor", "GitHub CLI 必须使用 repository 外固定绝对路径",
            )
        try:
            executable = self.gh_path.resolve(strict=True)
        except OSError as exc:
            raise VerificationProviderError(
                "attestor_unavailable", "受保护 policy 指定的 GitHub CLI 不可用",
            ) from exc
        if executable == repository or repository in executable.parents or not executable.is_file():
            raise VerificationProviderError(
                "untrusted_attestor", "GitHub CLI 必须是 repository 外的固定可执行文件",
            )
        if hashlib.sha256(executable.read_bytes()).hexdigest() != self.gh_sha256:
            raise VerificationProviderError("policy_downgrade", "GitHub CLI 内容摘要不匹配")

    @property
    def certificate_identity(self) -> str:
        return (
            f"https://github.com/{self.repository}/{self.workflow_path}@{self.workflow_ref}"
        )


@dataclass(frozen=True, slots=True)
class GitHubOIDCAttestationEnvelope:
    schema_version: int
    algorithm: str
    repository: str
    workflow_path: str
    workflow_ref: str
    attestor_run_id: str
    attestor_run_attempt: int
    subject_sha256: str
    payload: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "algorithm": self.algorithm,
            "repository": self.repository,
            "workflow_path": self.workflow_path,
            "workflow_ref": self.workflow_ref,
            "attestor_run_id": self.attestor_run_id,
            "attestor_run_attempt": self.attestor_run_attempt,
            "subject_sha256": self.subject_sha256,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> GitHubOIDCAttestationEnvelope:
        _strict(
            value,
            {
                "schema_version", "algorithm", "repository", "workflow_path", "workflow_ref",
                "attestor_run_id", "attestor_run_attempt", "subject_sha256", "payload",
            },
            "GitHub OIDC envelope",
        )
        payload = value["payload"]
        attempt = value["attestor_run_attempt"]
        schema_version = value["schema_version"]
        if (
            not isinstance(payload, Mapping)
            or type(schema_version) is not int
            or type(attempt) is not int
            or attempt < 1
        ):
            raise VerificationProviderError("invalid_attestation", "GitHub OIDC envelope 无效")
        return cls(
            schema_version, _text(value["algorithm"], "algorithm"),
            _text(value["repository"], "repository"),
            _text(value["workflow_path"], "workflow_path"),
            _text(value["workflow_ref"], "workflow_ref"),
            _text(value["attestor_run_id"], "attestor_run_id"), attempt,
            _sha(value["subject_sha256"], "subject_sha256", sha256=True),
            cast(Mapping[str, object], payload),
        )


@dataclass(frozen=True, slots=True)
class GitHubAttestationArtifact:
    """A strictly parsed result downloaded from one unique attestor run."""

    plan: VerificationPlan
    receipt: VerificationReceipt
    envelope: GitHubOIDCAttestationEnvelope
    attestor_run_id: str
    attestor_run_attempt: int
    source_url: str


class GitHubAttestorClient:
    """Dispatch and consume the protected main-only Phase 4 attestor."""

    def __init__(
        self,
        policy: GitHubAttestationTrustPolicy,
        *,
        command_runner: CommandRunner = _run_command,
        timeout_seconds: int = 900,
        poll_interval_seconds: int = 2,
        request_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds < 1 or poll_interval_seconds < 1:
            raise ValueError("GitHub attestor timeout/poll interval 必须为正整数")
        self.policy = policy
        self._command_runner = command_runner
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._request_id_factory = request_id_factory
        self._sleeper = sleeper

    def execute(
        self,
        *,
        suite_id: str,
        candidate_sha: str,
        execution_kind: str,
        ci_workflow: str | None = None,
        ci_event: str | None = None,
    ) -> GitHubAttestationArtifact:
        if _SHA1.fullmatch(candidate_sha) is None:
            raise VerificationProviderError("invalid_request", "candidate SHA 无效")
        request_id = self._request_id_factory()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}", request_id) is None:
            raise VerificationProviderError("invalid_request", "GitHub attestor request_id 无效")
        fields = {
            "candidate_sha": candidate_sha,
            "suite_id": suite_id,
            "request_id": request_id,
        }
        if execution_kind == "github-actions":
            ci_run_id, ci_attempt = self._select_ci_run(
                candidate_sha, workflow=ci_workflow, event=ci_event,
            )
            fields.update(ci_run_id=ci_run_id, ci_run_attempt=str(ci_attempt))
        elif execution_kind != "command":
            raise VerificationProviderError("invalid_request", "attested suite kind 不受支持")

        workflow_ref = self.policy.workflow_ref.removeprefix("refs/heads/")
        argv = [
            str(self.policy.gh_path), "workflow", "run", self.policy.workflow_path,
            "--repo", self.policy.repository, "--ref", workflow_ref,
        ]
        for key, value in fields.items():
            argv.extend(("-f", f"{key}={value}"))
        self._run(argv, operation="dispatch")

        title = f"phase4-attestation-{request_id}"
        run = self._locate_unique_run(title)
        run_id, attempt = self._run_identity(run)
        self._run(
            (
                str(self.policy.gh_path), "run", "watch", run_id,
                "--repo", self.policy.repository, "--exit-status",
            ),
            operation="wait",
        )
        final = self._view_run(run_id, attempt)
        source_url = self._require_final_run(final, title, run_id, attempt)

        with tempfile.TemporaryDirectory(prefix="ai-dev-os-attestor-download-") as temp:
            root = Path(temp)
            artifact_name = f"phase4-attestation-{run_id}-{attempt}"
            self._run(
                (
                    str(self.policy.gh_path), "run", "download", run_id,
                    "--repo", self.policy.repository, "--name", artifact_name,
                    "--dir", str(root),
                ),
                operation="download",
            )
            plan_bytes, raw_plan = self._strict_json_file(root, "phase4-plan.json")
            receipt_bytes, raw_receipt = self._strict_json_file(root, "phase4-receipt.json")
            plan = self._parse_plan(raw_plan)
            receipt = VerificationReceipt.from_dict(raw_receipt)
            if plan_bytes != _canonical(plan.to_dict()) or receipt_bytes != _canonical(receipt.to_dict()):
                raise VerificationProviderError(
                    "invalid_artifact", "attestor plan/receipt 不是唯一 canonical JSON",
                )
            envelope = GitHubOIDCAttestationEnvelope(
                schema_version=1,
                algorithm="sigstore-github-oidc",
                repository=self.policy.repository,
                workflow_path=self.policy.workflow_path,
                workflow_ref=self.policy.workflow_ref,
                attestor_run_id=run_id,
                attestor_run_attempt=attempt,
                subject_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
                payload=receipt.to_dict(),
            )
        return GitHubAttestationArtifact(
            plan, receipt, envelope, run_id, attempt, source_url,
        )

    def _select_ci_run(
        self, candidate_sha: str, *, workflow: str | None, event: str | None,
    ) -> tuple[str, int]:
        if not workflow or not event:
            raise VerificationProviderError("invalid_request", "GitHub CI suite 身份不完整")
        payload = self._json(
            (
                str(self.policy.gh_path), "run", "list", "--repo", self.policy.repository,
                "--workflow", workflow, "--commit", candidate_sha, "--event", event,
                "--status", "success", "--limit", "100", "--json",
                "databaseId,attempt,event,headSha,status,conclusion",
            ),
            operation="ci-list",
        )
        if not isinstance(payload, list):
            raise VerificationProviderError("invalid_response", "gh run list 未返回数组")
        matches = [
            item for item in payload
            if isinstance(item, Mapping)
            and item.get("headSha") == candidate_sha
            and item.get("event") == event
            and item.get("status") == "completed"
            and item.get("conclusion") == "success"
        ]
        if not matches:
            raise VerificationProviderError("verification_unavailable", "没有可供 attestor 复验的 CI run")
        identities = [self._run_identity(item) for item in matches]
        return max(identities, key=lambda item: int(item[0]))

    def _locate_unique_run(self, title: str) -> Mapping[str, object]:
        attempts = max(1, (self._timeout + self._poll_interval - 1) // self._poll_interval)
        for index in range(attempts):
            payload = self._json(
                (
                    str(self.policy.gh_path), "run", "list", "--repo", self.policy.repository,
                    "--workflow", self.policy.workflow_path, "--event", "workflow_dispatch",
                    "--branch", "main", "--limit", "100", "--json",
                    "databaseId,attempt,displayTitle,event,headBranch,status,conclusion,url",
                ),
                operation="locate",
            )
            if not isinstance(payload, list):
                raise VerificationProviderError("invalid_response", "gh run list 未返回数组")
            matches = [
                item for item in payload
                if isinstance(item, Mapping) and item.get("displayTitle") == title
            ]
            if len(matches) > 1:
                raise VerificationProviderError("ambiguous_run", "request_id 匹配到多个 attestor run")
            if len(matches) == 1:
                return cast(Mapping[str, object], matches[0])
            if index + 1 < attempts:
                self._sleeper(self._poll_interval)
        raise VerificationProviderError("attestor_timeout", "等待 request_id 对应 attestor run 超时")

    def _view_run(self, run_id: str, attempt: int) -> Mapping[str, object]:
        payload = self._json(
            (
                str(self.policy.gh_path), "run", "view", run_id,
                "--repo", self.policy.repository, "--attempt", str(attempt), "--json",
                "databaseId,attempt,displayTitle,event,headBranch,status,conclusion,url",
            ),
            operation="view",
        )
        if not isinstance(payload, Mapping):
            raise VerificationProviderError("invalid_response", "gh run view 未返回对象")
        return cast(Mapping[str, object], payload)

    def _require_final_run(
        self, run: Mapping[str, object], title: str, run_id: str, attempt: int,
    ) -> str:
        observed_id, observed_attempt = self._run_identity(run)
        url = run.get("url")
        base_url = f"https://github.com/{self.policy.repository}/actions/runs/{run_id}"
        expected_urls = {base_url, f"{base_url}/attempts/{attempt}"}
        if (
            observed_id != run_id
            or observed_attempt != attempt
            or run.get("displayTitle") != title
            or run.get("event") != "workflow_dispatch"
            or run.get("headBranch") != "main"
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or url not in expected_urls
        ):
            raise VerificationProviderError("stale_verification", "attestor run 最终事实不匹配")
        return url

    @staticmethod
    def _run_identity(run: Mapping[str, object]) -> tuple[str, int]:
        run_id = str(run.get("databaseId") or "")
        attempt = run.get("attempt")
        if _RUN_ID.fullmatch(run_id) is None or type(attempt) is not int or attempt < 1:
            raise VerificationProviderError("invalid_response", "GitHub run identity 无效")
        return run_id, attempt

    def _run(self, argv: Sequence[str], *, operation: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._command_runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise VerificationProviderError("attestor_timeout", f"GitHub attestor {operation} 超时") from exc
        except OSError as exc:
            raise VerificationProviderError("attestor_unavailable", f"GitHub attestor {operation} 不可用") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-1000:]
            raise VerificationProviderError(
                "attestor_failed", f"GitHub attestor {operation} 失败：{detail}",
            )
        return completed

    def _json(self, argv: Sequence[str], *, operation: str) -> object:
        completed = self._run(argv, operation=operation)
        try:
            return json.loads(completed.stdout, object_pairs_hook=self._unique_object)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VerificationProviderError("invalid_response", f"GitHub attestor {operation} JSON 无效") from exc

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    @classmethod
    def _strict_json_file(
        cls, root: Path, filename: str,
    ) -> tuple[bytes, Mapping[str, object]]:
        path = root / filename
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise VerificationProviderError("invalid_artifact", f"attestor artifact {filename} 缺失或无效")
        raw = path.read_bytes()
        try:
            value = json.loads(raw, object_pairs_hook=cls._unique_object)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise VerificationProviderError("invalid_artifact", f"attestor artifact {filename} JSON 无效") from exc
        if not isinstance(value, Mapping):
            raise VerificationProviderError("invalid_artifact", f"attestor artifact {filename} 不是对象")
        return raw, cast(Mapping[str, object], value)

    @staticmethod
    def _parse_plan(value: Mapping[str, object]) -> VerificationPlan:
        expected = {
            "project_id", "requirement_id", "phase", "candidate_sha", "candidate_tree",
            "policy_fingerprint", "environment_digest", "suites", "required_suite_ids", "mode",
        }
        if set(value) != expected:
            raise VerificationProviderError("invalid_artifact", "Verification Plan 字段无效")
        raw_suites = value.get("suites")
        raw_required = value.get("required_suite_ids")
        if not isinstance(raw_suites, list) or not isinstance(raw_required, list):
            raise VerificationProviderError("invalid_artifact", "Verification Plan suites 无效")
        suites: list[VerificationSuite] = []
        suite_fields = {
            "suite_id", "suite_type", "argv", "timeout_seconds", "cwd",
            "environment_allowlist", "environment", "artifacts", "requires_network",
            "network_reader_id",
        }
        artifact_fields = {"path", "required", "max_bytes"}
        try:
            for raw_suite in raw_suites:
                if not isinstance(raw_suite, Mapping) or set(raw_suite) != suite_fields:
                    raise TypeError
                raw_artifacts = raw_suite["artifacts"]
                environment = raw_suite["environment"]
                argv = raw_suite["argv"]
                allowlist = raw_suite["environment_allowlist"]
                timeout = raw_suite["timeout_seconds"]
                reader = raw_suite["network_reader_id"]
                if (
                    not isinstance(raw_artifacts, list)
                    or not isinstance(environment, Mapping)
                    or any(not isinstance(key, str) or not isinstance(item, str)
                           for key, item in environment.items())
                    or not isinstance(argv, list)
                    or any(not isinstance(item, str) for item in argv)
                    or not isinstance(allowlist, list)
                    or any(not isinstance(item, str) for item in allowlist)
                    or isinstance(timeout, bool)
                    or not isinstance(timeout, int)
                    or not isinstance(raw_suite["suite_id"], str)
                    or not isinstance(raw_suite["suite_type"], str)
                    or not isinstance(raw_suite["cwd"], str)
                    or not isinstance(raw_suite["requires_network"], bool)
                    or reader is not None and not isinstance(reader, str)
                ):
                    raise TypeError
                artifacts = []
                for raw_artifact in raw_artifacts:
                    if not isinstance(raw_artifact, Mapping) or set(raw_artifact) != artifact_fields:
                        raise TypeError
                    path = raw_artifact["path"]
                    required = raw_artifact["required"]
                    max_bytes = raw_artifact["max_bytes"]
                    if (
                        not isinstance(path, str)
                        or not isinstance(required, bool)
                        or max_bytes is not None
                        and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int))
                    ):
                        raise TypeError
                    artifacts.append(ArtifactConstraint(
                        path, required, max_bytes,
                    ))
                suites.append(VerificationSuite(
                    suite_id=raw_suite["suite_id"],
                    suite_type=cast(Any, raw_suite["suite_type"]),
                    argv=tuple(argv),
                    timeout_seconds=timeout,
                    cwd=raw_suite["cwd"],
                    environment_allowlist=tuple(allowlist),
                    environment=cast(Mapping[str, str], environment),
                    artifacts=tuple(artifacts),
                    requires_network=raw_suite["requires_network"],
                    network_reader_id=reader,
                ))
            top_strings = (
                "project_id", "requirement_id", "candidate_sha", "candidate_tree",
                "policy_fingerprint", "environment_digest", "mode",
            )
            if (
                any(not isinstance(value[name], str) for name in top_strings)
                or isinstance(value["phase"], bool)
                or not isinstance(value["phase"], int)
                or any(not isinstance(item, str) for item in raw_required)
            ):
                raise TypeError
            plan = VerificationPlan(
                cast(str, value["project_id"]), cast(str, value["requirement_id"]),
                value["phase"], cast(str, value["candidate_sha"]),
                cast(str, value["candidate_tree"]), cast(str, value["policy_fingerprint"]),
                cast(str, value["environment_digest"]), tuple(suites),
                tuple(cast(list[str], raw_required)), cast(Any, value["mode"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VerificationProviderError("invalid_artifact", "Verification Plan 内容无效") from exc
        if plan.to_dict() != dict(value):
            raise VerificationProviderError("invalid_artifact", "Verification Plan 类型或内容无效")
        return plan


class GitHubAttestationVerifier:
    """使用 GitHub CLI 的 Sigstore 实现，并用实时 API 收紧 workflow/run 身份。"""

    def __init__(
        self,
        policy: GitHubAttestationTrustPolicy,
        *,
        json_reader: JsonReader = _read_github_json,
        command_runner: CommandRunner = _run_command,
        timeout_seconds: int = 120,
    ) -> None:
        self.policy = policy
        self._json_reader = json_reader
        self._command_runner = command_runner
        self._timeout = timeout_seconds

    def verify(
        self,
        raw_envelope: Mapping[str, object],
        raw_plan: Mapping[str, object],
        expected_run_id: str,
        expected_attempt: int,
    ) -> Mapping[str, object]:
        envelope = GitHubOIDCAttestationEnvelope.from_dict(raw_envelope)
        self._require_envelope_identity(envelope)
        subject = _canonical(envelope.payload)
        subject_sha = hashlib.sha256(subject).hexdigest()
        if subject_sha != envelope.subject_sha256:
            raise VerificationProviderError("artifact_changed", "attested Receipt 内容摘要不匹配")
        self._require_receipt(envelope.payload, raw_plan, expected_run_id, expected_attempt)

        with tempfile.TemporaryDirectory(prefix="ai-dev-os-attestation-") as temp:
            artifact = Path(temp) / "phase4-receipt.json"
            artifact.write_bytes(subject)
            completed = self._verify_with_gh(artifact)
        verified = self._parse_verified(completed)
        certificate = self._matching_certificate(verified, envelope, subject_sha)
        workflow_sha = self._require_certificate(certificate, envelope)
        self._require_live_github(envelope, workflow_sha)
        return dict(envelope.payload)

    def _require_envelope_identity(self, envelope: GitHubOIDCAttestationEnvelope) -> None:
        if (
            envelope.schema_version != 1
            or envelope.algorithm != "sigstore-github-oidc"
            or envelope.repository != self.policy.repository
            or envelope.workflow_path != self.policy.workflow_path
            or envelope.workflow_ref != self.policy.workflow_ref
            or _RUN_ID.fullmatch(envelope.attestor_run_id) is None
        ):
            raise VerificationProviderError("untrusted_attestor", "GitHub attestor identity 不匹配")

    def _verify_with_gh(self, artifact: Path) -> subprocess.CompletedProcess[str]:
        argv = (
            str(self.policy.gh_path), "attestation", "verify", str(artifact),
            "--repo", self.policy.repository,
            "--cert-identity", self.policy.certificate_identity,
            "--cert-oidc-issuer", _OIDC_ISSUER,
            "--source-ref", self.policy.workflow_ref,
            "--predicate-type", _PROVENANCE_PREDICATE,
            "--deny-self-hosted-runners", "--hostname", "github.com", "--format", "json",
        )
        try:
            completed = self._command_runner(argv, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VerificationProviderError(
                "attestor_unavailable", "GitHub CLI attestation verifier 不可用",
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-1000:]
            raise VerificationProviderError(
                "invalid_signature", f"GitHub/Sigstore attestation 验证失败：{detail}",
            )
        return completed

    @staticmethod
    def _parse_verified(completed: subprocess.CompletedProcess[str]) -> list[Mapping[str, object]]:
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VerificationProviderError("invalid_attestation", "GitHub CLI 返回无效 JSON") from exc
        if not isinstance(payload, list) or not payload or any(
            not isinstance(item, Mapping) for item in payload
        ):
            raise VerificationProviderError("invalid_attestation", "GitHub CLI 未返回已验证 attestation")
        return cast(list[Mapping[str, object]], payload)

    def _matching_certificate(
        self,
        verified: list[Mapping[str, object]],
        envelope: GitHubOIDCAttestationEnvelope,
        subject_sha: str,
    ) -> Mapping[str, object]:
        expected_invocation = (
            f"https://github.com/{self.policy.repository}/actions/runs/"
            f"{envelope.attestor_run_id}/attempts/{envelope.attestor_run_attempt}"
        )
        for item in verified:
            result = item.get("verificationResult")
            if not isinstance(result, Mapping):
                continue
            signature, statement = result.get("signature"), result.get("statement")
            timestamps = result.get("verifiedTimestamps")
            if not isinstance(signature, Mapping) or not isinstance(statement, Mapping):
                continue
            certificate = signature.get("certificate")
            if not isinstance(certificate, Mapping) or certificate.get("runInvocationURI") != expected_invocation:
                continue
            subjects = statement.get("subject")
            if not isinstance(subjects, list) or not any(
                isinstance(subject, Mapping)
                and isinstance(subject.get("digest"), Mapping)
                and subject["digest"].get("sha256") == subject_sha
                for subject in subjects
            ):
                continue
            if statement.get("predicateType") != _PROVENANCE_PREDICATE:
                continue
            if not isinstance(timestamps, list) or not timestamps:
                continue
            return cast(Mapping[str, object], certificate)
        raise VerificationProviderError(
            "context_replay", "没有 attestation 同时绑定 Receipt subject 与目标 run/attempt",
        )

    def _require_certificate(
        self,
        certificate: Mapping[str, object],
        envelope: GitHubOIDCAttestationEnvelope,
    ) -> str:
        workflow_sha = _sha(certificate.get("buildSignerDigest"), "buildSignerDigest")
        expected_invocation = (
            f"https://github.com/{self.policy.repository}/actions/runs/"
            f"{envelope.attestor_run_id}/attempts/{envelope.attestor_run_attempt}"
        )
        expected = {
            "subjectAlternativeName": self.policy.certificate_identity,
            "issuer": _OIDC_ISSUER,
            "buildSignerURI": self.policy.certificate_identity,
            "buildSignerDigest": workflow_sha,
            "runnerEnvironment": "github-hosted",
            "sourceRepositoryURI": f"https://github.com/{self.policy.repository}",
            "sourceRepositoryDigest": workflow_sha,
            "sourceRepositoryRef": self.policy.workflow_ref,
            "buildConfigURI": self.policy.certificate_identity,
            "buildConfigDigest": workflow_sha,
            "buildTrigger": "workflow_dispatch",
            "runInvocationURI": expected_invocation,
        }
        if any(certificate.get(name) != value for name, value in expected.items()):
            raise VerificationProviderError("untrusted_attestor", "OIDC certificate identity 不匹配")
        return workflow_sha

    def _require_live_github(
        self, envelope: GitHubOIDCAttestationEnvelope, workflow_sha: str,
    ) -> None:
        base = f"https://api.github.com/repos/{self.policy.repository}"
        run_url = (
            f"{base}/actions/runs/{envelope.attestor_run_id}/attempts/"
            f"{envelope.attestor_run_attempt}"
        )
        run = self._json_reader(run_url)
        repository = run.get("repository")
        if (
            str(run.get("id")) != envelope.attestor_run_id
            or run.get("run_attempt") != envelope.attestor_run_attempt
            or run.get("head_sha") != workflow_sha
            or run.get("head_branch") != "main"
            or run.get("path") != self.policy.workflow_path
            or run.get("event") != "workflow_dispatch"
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or not isinstance(repository, Mapping)
            or repository.get("full_name") != self.policy.repository
        ):
            raise VerificationProviderError("stale_verification", "GitHub Actions run 实时事实不匹配")

        commit = self._json_reader(f"{base}/git/commits/{envelope.payload['candidate_sha']}")
        tree = commit.get("tree")
        if (
            commit.get("sha") != envelope.payload["candidate_sha"]
            or not isinstance(tree, Mapping)
            or tree.get("sha") != envelope.payload["candidate_tree"]
        ):
            raise VerificationProviderError("stale_verification", "候选 exact SHA/tree 实时事实不匹配")

        for path, expected_sha in (
            (self.policy.workflow_path, self.policy.workflow_sha256),
            (self.policy.runner_path, self.policy.runner_sha256),
        ):
            raw = self._remote_content(base, path, workflow_sha)
            if hashlib.sha256(raw).hexdigest() != expected_sha:
                raise VerificationProviderError("policy_downgrade", f"受保护 attestor 文件已变化：{path}")
            if path == self.policy.workflow_path and (
                f"actions/attest-build-provenance@{self.policy.action_sha}".encode() not in raw
            ):
                raise VerificationProviderError("policy_downgrade", "attestation action 未固定到受信 commit")

        raw_policy = self._remote_content(
            base, self.policy.attestor_policy_path, workflow_sha,
        )
        try:
            policy = json.loads(raw_policy)
            if not isinstance(policy, Mapping):
                raise TypeError
            claimed = policy.get("policy_fingerprint")
            unsigned = dict(policy)
            unsigned.pop("policy_fingerprint", None)
        except (json.JSONDecodeError, TypeError) as exc:
            raise VerificationProviderError(
                "policy_downgrade", "受保护 attestor policy 内容无效",
            ) from exc
        if (
            claimed != self.policy.attestor_policy_fingerprint
            or fingerprint(unsigned) != self.policy.attestor_policy_fingerprint
        ):
            raise VerificationProviderError("policy_downgrade", "attestor policy 指纹不匹配")

    def _remote_content(self, base: str, path: str, workflow_sha: str) -> bytes:
        query = urlencode({"ref": workflow_sha})
        content = self._json_reader(f"{base}/contents/{quote(path, safe='/')}?{query}")
        encoded = content.get("content")
        if content.get("encoding") != "base64" or not isinstance(encoded, str):
            raise VerificationProviderError("attestor_offline", "GitHub contents API 返回无效内容")
        try:
            return base64.b64decode("".join(encoded.split()), validate=True)
        except ValueError as exc:
            raise VerificationProviderError("attestor_offline", "GitHub contents API base64 无效") from exc

    def _require_receipt(
        self,
        payload: Mapping[str, object],
        plan: Mapping[str, object],
        expected_run_id: str,
        expected_attempt: int,
    ) -> None:
        receipt = VerificationReceipt.from_dict(payload)
        expected = {
            "project_id": plan.get("project_id"),
            "requirement_id": plan.get("requirement_id"),
            "phase": plan.get("phase"),
            "candidate_sha": plan.get("candidate_sha"),
            "candidate_tree": plan.get("candidate_tree"),
            "policy_fingerprint": self.policy.attestor_policy_fingerprint,
            "environment_digest": plan.get("environment_digest"),
            "run_id": expected_run_id,
            "attempt": expected_attempt,
        }
        normalized = receipt.to_dict()
        if (
            any(normalized.get(name) != value for name, value in expected.items())
            or receipt.plan_fingerprint != fingerprint(plan)
            or plan.get("policy_fingerprint") != self.policy.attestor_policy_fingerprint
        ):
            raise VerificationProviderError("context_replay", "Receipt/plan/policy 上下文不匹配")
        suites, results = plan.get("suites"), normalized.get("results")
        if not isinstance(suites, list) or not isinstance(results, list) or not suites:
            raise VerificationProviderError("invalid_receipt", "Receipt/plan suite 无效")
        if tuple(item.get("suite_id") for item in suites if isinstance(item, Mapping)) != tuple(
            item.get("suite_id") for item in results if isinstance(item, Mapping)
        ):
            raise VerificationProviderError("invalid_receipt", "Receipt suite 未精确覆盖 plan")
        required = plan.get("required_suite_ids")
        if not isinstance(required, list) or receipt.result != "PASS" or any(
            not any(
                isinstance(item, Mapping)
                and item.get("suite_id") == suite_id
                and item.get("status") == "PASS"
                for item in results
            )
            for suite_id in required
        ):
            raise VerificationProviderError("verification_failed", "必需 suite 未全部 PASS")
        artifacts = [
            artifact
            for result in results
            if isinstance(result, Mapping) and isinstance(result.get("artifacts"), list)
            for artifact in cast(list[object], result["artifacts"])
            if isinstance(artifact, Mapping)
        ]
        if receipt.artifact_digest != fingerprint(artifacts):
            raise VerificationProviderError("artifact_changed", "Receipt artifact digest 不匹配")
