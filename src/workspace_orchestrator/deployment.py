"""Phase 6 main-only 部署门禁的最小可用实现。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from .integration.contracts import MergeReceipt
from .integration.git_workspace import GitWorkspaceError, TrustedGit
from .workspace import WorkspaceError, WorkspaceStore, _file_lock


class DeploymentError(WorkspaceError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeploymentPolicy:
    environment: str
    provider_version: str
    require_remote_main: bool = True
    project_id: str = "project"
    provider_id: str = "default"
    timeout_seconds: float = 900
    deployment_required: bool = True


@dataclass(frozen=True, slots=True)
class DeploymentAuthorization:
    authorization_id: str
    requirement_id: str
    environment: str
    commit_sha: str
    merge_receipt_id: str
    verification_receipt_id: str
    requested_by: str = "system"
    issued_at: str = ""


@dataclass(frozen=True, slots=True)
class DeploymentAuthority:
    authorization: DeploymentAuthorization
    merge: MergeReceipt
    post_merge_verification_receipt_id: str


class DeploymentAuthorityProvider(Protocol):
    def resolve(self, authorization_id: str) -> DeploymentAuthority:
        """从权威持久存储解析不可变部署授权及其收据。"""


class DeploymentAuthorityStore:
    """部署控制器写入、DeploymentService 只读解析的不可变授权存储。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def record(self, authority: DeploymentAuthority) -> Path:
        self._validate(authority)
        path = self._path(authority.authorization.authorization_id)
        document = {
            "authorization": asdict(authority.authorization),
            "merge": authority.merge.to_dict(),
            "post_merge_verification_receipt_id": (
                authority.post_merge_verification_receipt_id
            ),
        }
        with _file_lock(path.with_suffix(".lock")):
            if path.is_file():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise DeploymentError("authority_corrupt", "部署授权记录损坏") from exc
                if existing != document:
                    raise DeploymentError("authority_conflict", "部署授权 ID 已绑定不同内容")
                return path
            WorkspaceStore.write_json(path, document)
        return path

    def resolve(self, authorization_id: str) -> DeploymentAuthority:
        path = self._path(authorization_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise TypeError("记录不是对象")
            raw_auth = document["authorization"]
            raw_merge = document["merge"]
            verification = document["post_merge_verification_receipt_id"]
            if not isinstance(raw_auth, dict) or not isinstance(raw_merge, dict):
                raise TypeError("凭据不是对象")
            if set(raw_auth) != {field.name for field in fields(DeploymentAuthorization)}:
                raise TypeError("授权字段不完整")
            authority = DeploymentAuthority(
                DeploymentAuthorization(**raw_auth),
                MergeReceipt.from_dict(raw_merge),
                str(verification),
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DeploymentError("authority_missing", "部署授权缺失或损坏") from exc
        self._validate(authority)
        if authority.authorization.authorization_id != authorization_id:
            raise DeploymentError("authority_mismatch", "部署授权 ID 不一致")
        return authority

    def _path(self, authorization_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", authorization_id) is None:
            raise DeploymentError("invalid_authorization", "部署授权 ID 不是安全标识符")
        return self.root / f"{authorization_id}.json"

    @staticmethod
    def _validate(authority: DeploymentAuthority) -> None:
        auth, merge = authority.authorization, authority.merge
        merge.validate()
        if (
            not authority.post_merge_verification_receipt_id
            or auth.requirement_id != merge.requirement_id
            or auth.commit_sha != merge.merged_sha
            or auth.merge_receipt_id != merge.receipt_id
            or auth.verification_receipt_id != authority.post_merge_verification_receipt_id
            or merge.post_merge_verification_receipt_id
            != authority.post_merge_verification_receipt_id
        ):
            raise DeploymentError("invalid_authorization", "部署授权与合并/验证收据不一致")


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
    project_id: str = "project"
    provider_id: str = "default"
    authorization_id: str = ""
    requested_by: str = "system"
    merge_receipt_id: str = ""
    verification_receipt_id: str = ""
    main_proof: dict[str, str | bool | None] | None = None
    integrity: str = ""


@dataclass(frozen=True, slots=True)
class CompletionToken:
    token_id: str
    requirement_id: str
    commit_sha: str
    deployment_receipt_id: str | None
    issued_at: str
    deployment_required: bool
    environment: str


@dataclass(frozen=True, slots=True)
class DeploymentEnvironment:
    name: str
    provider_id: str
    provider_version: str
    deployment_required: bool = True
    require_remote_main: bool = True
    timeout_seconds: float = 900


class DeploymentEnvironmentRegistry:
    """人类可读的环境注册表；只保存非敏感 Provider 元数据。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def register(self, environment: DeploymentEnvironment) -> DeploymentEnvironment:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", environment.name) is None:
            raise DeploymentError("invalid_environment", "环境名称不是安全标识符")
        if environment.timeout_seconds <= 0:
            raise DeploymentError("invalid_environment", "部署超时必须大于零")
        for value, label in (
            (environment.provider_id, "Provider"),
            (environment.provider_version, "Provider 版本"),
        ):
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
                raise DeploymentError("invalid_environment", f"{label} 不是安全标识符")
        with _file_lock(self.path.with_suffix(".lock")):
            rows = self._read()
            previous = rows.get(environment.name)
            document = asdict(environment)
            if previous is not None and previous != document:
                raise DeploymentError("environment_conflict", "环境已注册且配置不同")
            rows[environment.name] = document
            self._write(rows)
        return environment

    def get(self, name: str) -> DeploymentEnvironment:
        document = self._read().get(name)
        if document is None:
            raise DeploymentError("environment_missing", "部署环境未注册")
        try:
            required = document["deployment_required"]
            remote = document["require_remote_main"]
            timeout = document["timeout_seconds"]
            if type(required) is not bool or type(remote) is not bool:
                raise TypeError("布尔字段无效")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise TypeError("超时字段无效")
            return DeploymentEnvironment(
                name=str(document["name"]),
                provider_id=str(document["provider_id"]),
                provider_version=str(document["provider_version"]),
                deployment_required=required,
                require_remote_main=remote,
                timeout_seconds=float(timeout),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DeploymentError("registry_corrupt", "部署环境注册表字段损坏") from exc

    def _read(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise DeploymentError("registry_corrupt", "部署环境注册表损坏")
        if any(not isinstance(key, str) or not isinstance(row, dict)
               for key, row in value.items()):
            raise DeploymentError("registry_corrupt", "部署环境注册表条目损坏")
        return value

    def _write(self, rows: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class DeploymentProvider(Protocol):
    def deploy(
        self, *, environment: str, commit_sha: str, timeout_seconds: float,
    ) -> tuple[bool, str, str]:
        """返回 success、detail、rollback。"""


class DryRunDeploymentProvider:
    """不执行外部副作用的真实本地接线验证 Provider。"""

    def deploy(
        self, *, environment: str, commit_sha: str, timeout_seconds: float,
    ) -> tuple[bool, str, str]:
        if timeout_seconds <= 0:
            raise TimeoutError("dry-run 超时配置无效")
        if re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
            raise RuntimeError("dry-run 收到无效提交")
        return True, f"dry-run verified {environment}@{commit_sha}", "无需回滚"


class MainStateProvider(Protocol):
    def state(self) -> tuple[str | None, str | None, bool, str]:
        """返回 branch、remote main SHA、clean、local HEAD。"""


class DeploymentService:
    def __init__(self, root: Path, policy: DeploymentPolicy, main: MainStateProvider,
                 provider: DeploymentProvider, authority: DeploymentAuthorityProvider) -> None:
        if policy.timeout_seconds <= 0:
            raise DeploymentError("invalid_policy", "部署超时必须大于零")
        for value, label in (
            (policy.environment, "环境"),
            (policy.project_id, "项目"),
            (policy.provider_id, "Provider"),
            (policy.provider_version, "Provider 版本"),
        ):
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
                raise DeploymentError("invalid_policy", f"{label}不是安全标识符")
        self.root, self.policy, self.main = root, policy, main
        self.provider, self.authority = provider, authority

    @classmethod
    def from_registry(
        cls,
        root: Path,
        *,
        project_id: str,
        environment: str,
        registry: DeploymentEnvironmentRegistry,
        main: MainStateProvider,
        providers: Mapping[str, DeploymentProvider],
        authority: DeploymentAuthorityProvider,
    ) -> DeploymentService:
        configured = registry.get(environment)
        provider = providers.get(configured.provider_id)
        if provider is None:
            raise DeploymentError("provider_unavailable", "部署 Provider 未注册")
        policy = DeploymentPolicy(
            configured.name,
            configured.provider_version,
            configured.require_remote_main,
            project_id,
            configured.provider_id,
            configured.timeout_seconds,
            configured.deployment_required,
        )
        return cls(root, policy, main, provider, authority)

    def deploy(self, authorization: DeploymentAuthorization, merge: MergeReceipt,
               *, post_merge_verification_receipt_id: str) -> DeploymentReceipt:
        authoritative = self.authority.resolve(authorization.authorization_id)
        if authoritative != DeploymentAuthority(
            authorization, merge, post_merge_verification_receipt_id,
        ):
            raise DeploymentError("authority_mismatch", "部署凭据与权威持久记录不一致")
        self._validate(authorization, merge, post_merge_verification_receipt_id)
        key = (
            f"{self.policy.project_id}-{self.policy.environment}-{authorization.commit_sha}-"
            f"{self.policy.provider_version}"
        )
        path = self.root / f"{key}.json"
        with _file_lock(path.with_suffix(".lock")):
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise DeploymentError("receipt_corrupt", "部署收据损坏") from exc
                if not isinstance(existing, dict):
                    raise DeploymentError("receipt_corrupt", "部署收据损坏")
                if existing.get("status") == "in_progress":
                    raise DeploymentError(
                        "deployment_uncertain",
                        "上次部署在副作用后未写回结果，必须人工核对 Provider 状态",
                    )
                try:
                    receipt = DeploymentReceipt(**existing)
                except (TypeError, ValueError) as exc:
                    raise DeploymentError("receipt_corrupt", "部署收据字段损坏") from exc
                self._validate_existing_receipt(
                    receipt, authorization, merge, post_merge_verification_receipt_id,
                )
                return receipt
            # 真正副作用之前重查，关闭授权检查与执行之间的漂移窗口。
            self._validate(authorization, merge, post_merge_verification_receipt_id)
            started = datetime.now(UTC).isoformat()
            self._write(path, {
                "status": "in_progress",
                "authorization_id": authorization.authorization_id,
                "requirement_id": authorization.requirement_id,
                "commit_sha": authorization.commit_sha,
                "started_at": started,
            })
            try:
                success, detail, rollback = self.provider.deploy(
                    environment=self.policy.environment,
                    commit_sha=authorization.commit_sha,
                    timeout_seconds=self.policy.timeout_seconds,
                )
            except (OSError, RuntimeError, TimeoutError) as exc:
                success, detail, rollback = False, f"Provider unavailable: {exc}", ""
            branch, remote_sha, clean, head = self.main.state()
            receipt = DeploymentReceipt(
                f"deployment-{uuid4().hex}", authorization.requirement_id,
                self.policy.environment, authorization.commit_sha, self.policy.provider_version,
                "succeeded" if success else "failed", started, datetime.now(UTC).isoformat(),
                rollback, detail, self.policy.project_id, self.policy.provider_id,
                authorization.authorization_id, authorization.requested_by,
                merge.receipt_id, post_merge_verification_receipt_id,
                {"branch": branch, "remote_main_sha": remote_sha, "clean": clean, "head": head},
            )
            receipt = replace(receipt, integrity=self._sign_receipt(receipt))
            self._write(path, asdict(receipt))
            return receipt

    def complete(self, requirement_id: str, commit_sha: str,
                 *, receipt: DeploymentReceipt | None = None) -> CompletionToken:
        deployment_required = self.policy.deployment_required
        if deployment_required and (receipt is None or receipt.status != "succeeded"
                                    or receipt.commit_sha != commit_sha
                                    or receipt.requirement_id != requirement_id):
            raise DeploymentError("deployment_required", "缺少当前提交的成功部署收据")
        if deployment_required and receipt is not None and not self._receipt_is_persisted(receipt):
            raise DeploymentError("receipt_untrusted", "部署收据不是本服务持久化的成功结果")
        receipt_id = receipt.receipt_id if receipt else None
        key = (
            f"completion-{self.policy.environment}-{requirement_id}-{commit_sha}-"
            f"{receipt_id or 'not-required'}.json"
        )
        path = self.root / key
        with _file_lock(path.with_suffix(".lock")):
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    token = CompletionToken(**existing)
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise DeploymentError("completion_corrupt", "完成记录损坏") from exc
                if token.deployment_required != deployment_required:
                    raise DeploymentError("completion_conflict", "完成记录的部署策略不一致")
                return token
            token = CompletionToken(
                f"completion-{uuid4().hex}", requirement_id, commit_sha, receipt_id,
                datetime.now(UTC).isoformat(), deployment_required, self.policy.environment,
            )
            self._write(path, asdict(token))
            return token

    def _receipt_is_persisted(self, receipt: DeploymentReceipt) -> bool:
        if not self._receipt_integrity_valid(receipt):
            return False
        expected = asdict(receipt)
        for path in self.root.glob("*.json"):
            if path.name.startswith("completion-"):
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if document == expected:
                return True
        return False

    def _sign_receipt(self, receipt: DeploymentReceipt) -> str:
        payload = {**asdict(receipt), "integrity": ""}
        return hmac.new(
            self._integrity_key(),
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _receipt_integrity_valid(self, receipt: DeploymentReceipt) -> bool:
        return bool(receipt.integrity) and hmac.compare_digest(
            receipt.integrity, self._sign_receipt(receipt),
        )

    def _integrity_key(self) -> bytes:
        path = self.root / ".receipt-integrity.key"
        with _file_lock(path.with_suffix(".lock")):
            if path.is_file():
                key = path.read_bytes()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                key = secrets.token_bytes(32)
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                temporary.write_bytes(key)
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
        if len(key) != 32:
            raise DeploymentError("integrity_key_corrupt", "部署收据完整性密钥损坏")
        return key

    def _write(self, path: Path, document: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        os.replace(temporary, path)

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

    def _validate_existing_receipt(
        self,
        receipt: DeploymentReceipt,
        authorization: DeploymentAuthorization,
        merge: MergeReceipt,
        verification_receipt_id: str,
    ) -> None:
        expected = {
            "requirement_id": authorization.requirement_id,
            "environment": self.policy.environment,
            "commit_sha": authorization.commit_sha,
            "provider_version": self.policy.provider_version,
            "project_id": self.policy.project_id,
            "provider_id": self.policy.provider_id,
            "authorization_id": authorization.authorization_id,
            "requested_by": authorization.requested_by,
            "merge_receipt_id": merge.receipt_id,
            "verification_receipt_id": verification_receipt_id,
        }
        if not self._receipt_integrity_valid(receipt):
            raise DeploymentError("receipt_tampered", "部署收据完整性校验失败")
        if receipt.status not in {"succeeded", "failed"} or any(
            getattr(receipt, name) != value for name, value in expected.items()
        ):
            raise DeploymentError("receipt_mismatch", "部署收据与当前权威授权不一致")


def publish_completion_token(
    store: WorkspaceStore,
    token: CompletionToken,
    *,
    source_root: Path,
    registry: DeploymentEnvironmentRegistry,
) -> Path:
    """仅把部署服务已持久化且通过 Phase 6 exact-SHA Gate 的 Token 接入完成入口。"""

    source_name = (
        f"completion-{token.environment}-{token.requirement_id}-{token.commit_sha}-"
        f"{token.deployment_receipt_id or 'not-required'}.json"
    )
    source_path = source_root / source_name
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError("completion_untrusted", "CompletionToken 没有部署服务完成记录") from exc
    if source != asdict(token):
        raise DeploymentError("completion_untrusted", "CompletionToken 与部署服务完成记录不一致")
    environment = registry.get(token.environment)
    if environment.deployment_required != token.deployment_required:
        raise DeploymentError("completion_conflict", "CompletionToken 与权威环境部署策略不一致")
    if token.deployment_required and not token.deployment_receipt_id:
        raise DeploymentError("deployment_required", "CompletionToken 缺少部署收据")
    if not token.deployment_required and token.deployment_receipt_id is not None:
        raise DeploymentError("completion_conflict", "无需部署的 CompletionToken 不得绑定部署收据")

    with store.locked(token.requirement_id):
        workspace = store.path_for(token.requirement_id)
        gate_path = workspace / "phase-gates" / "phase-6.json"
        if not gate_path.is_file():
            raise DeploymentError("phase6_gate_missing", "缺少 Phase 6 Gate")
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError("phase6_gate_invalid", "Phase 6 Gate 损坏") from exc
        if not isinstance(gate, dict) or gate.get("status") != "PASS":
            raise DeploymentError("phase6_gate_stale", "CompletionToken 与 Phase 6 Gate 不一致")
        gate_sha = gate.get("commit_sha")
        if not isinstance(gate_sha, str) or (
            gate_sha != token.commit_sha
            and not _same_git_tree(store, gate_sha, token.commit_sha)
        ):
            raise DeploymentError("phase6_gate_stale", "CompletionToken 与 Phase 6 Gate 不一致")
        target = workspace / "deployment" / "completion-token.json"
        document = {
            "token": asdict(token),
            "source": str(source_path.resolve()),
            "environment_policy": asdict(environment),
        }
        if target.is_file():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing != document:
                raise DeploymentError("completion_conflict", "Requirement 已有不同 CompletionToken")
            return target
        store.write_json(target, document)
        return target


def _same_git_tree(store: WorkspaceStore, gate_sha: str, deployed_sha: str) -> bool:
    """允许 PR merge 改写 commit 身份，但绝不允许改变已通过 Gate 的源码树。"""

    if any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in (gate_sha, deployed_sha)):
        return False
    try:
        git = TrustedGit(store.working_root)
        trees = tuple(
            git.run("rev-parse", "--verify", "--end-of-options", sha + "^{tree}")
            for sha in (gate_sha, deployed_sha)
        )
    except GitWorkspaceError:
        return False
    return all(re.fullmatch(r"[0-9a-f]{40}", tree) is not None for tree in trees) and (
        trees[0] == trees[1]
    )
