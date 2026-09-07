from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from workspace_orchestrator.orchestration.contracts import (
    VerificationCommandResult,
    VerificationReceiptEnvelope,
    fingerprint,
)
from workspace_orchestrator.verification_provider import (
    ArtifactConstraint,
    AttestorPolicy,
    FakeVerificationProvider,
    LocalVerificationProvider,
    ProcessResult,
    ProtectedExecutionContext,
    ReceiptAttestor,
    RuleVerificationPlannerProvider,
    SignedReceiptEnvelope,
    TrustStore,
    VerificationPlan,
    VerificationProviderError,
    VerificationSuite,
    _canonical,
    migrate_legacy_receipt,
)

SHA = "a" * 40
TREE = "b" * 40


def _authority(tmp_path: Path, repository: Path, *, allow_legacy: bool = True,
               network_readers: tuple[str, ...] = ("trusted-github-api",)):
    authority = tmp_path / "authority"
    authority.mkdir()
    key = Ed25519PrivateKey.generate()
    private_path = authority / "key.pem"
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    policy = {
        "policy_id": "protected-policy-v1", "allowed_key_ids": ["key-1"],
        "allowed_suite_types": ["unit", "type", "lint", "integration", "e2e", "security", "custom"],
        "network_reader_ids": ["trusted-github-api"], "allow_legacy_migration": allow_legacy,
    }
    policy_path = authority / "policy.json"
    policy_path.write_text(json.dumps({**policy, "policy_fingerprint": fingerprint(policy)}), encoding="utf-8")
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    trust_path = authority / "trust.json"
    trust_path.write_text(json.dumps({
        "trust_store_id": "host-trust-v1", "attestors": {"controller": ["key-1"]},
        "keys": {"key-1": base64.b64encode(public).decode("ascii")},
    }), encoding="utf-8")
    attestor = ReceiptAttestor(
        repository=repository, policy_path=policy_path, private_key_path=private_path,
        key_id="key-1", attestor_id="controller", context_reader=lambda _plan: (
            ProtectedExecutionContext(SHA, TREE, fingerprint({"LANG": "C"}), network_readers)
        ),
    )
    return attestor, TrustStore.load(trust_path, repository=repository)


def _plan(policy: str, *suites: VerificationSuite, mode="collect-all") -> VerificationPlan:
    return VerificationPlan(
        "project", "REQ-020", 4, SHA, TREE, policy, fingerprint({"LANG": "C"}), tuple(suites),
        tuple(item.suite_id for item in suites), mode,
    )


def _suite(name="unit", **changes) -> VerificationSuite:
    values = {"suite_id": name, "suite_type": name if name != "check" else "custom",
              "argv": ("tool", "check")}
    values.update(changes)
    return VerificationSuite(**values)


def _local(**kwargs):
    kwargs.setdefault("candidate_identity", lambda _root: (SHA, TREE))
    return LocalVerificationProvider(**kwargs)


def test_local_provider_contract_covers_all_suite_types_and_artifact(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "report.xml").write_text("ok", encoding="utf-8")
    attestor, trust = _authority(tmp_path, repository)
    suites = tuple(
        _suite(kind, artifacts=(ArtifactConstraint("report.xml"),) if kind == "unit" else ())
        for kind in ("unit", "type", "lint", "integration", "e2e", "security", "custom")
    )
    calls = []

    def runner(argv, *, cwd, env, timeout):
        calls.append((argv, cwd, env, timeout))
        return ProcessResult(0, b"ok", b"")

    plan = _plan(attestor.policy.policy_fingerprint, *suites)
    receipt = _local(environment={"LANG": "C"}, runner=runner).execute(
        plan, workspace=repository, run_id="run-1", attempt=2,
    )
    envelope = attestor.sign(receipt, plan)
    verified = trust.verify(
        envelope, plan=plan, policy=attestor.policy,
        expected_run_id="run-1", expected_attempt=2,
    )
    assert verified["result"] == "PASS"
    assert len(calls) == 7
    assert receipt.results[0].artifacts[0].sha256


def test_rule_planner_freezes_policy_environment_and_required_suites():
    environment = {"LANG": "C"}
    plan = RuleVerificationPlannerProvider("c" * 64).plan(
        project_id="project", requirement_id="REQ-020", phase=4,
        candidate_sha=SHA, candidate_tree=TREE, environment=environment,
        suites=(_suite(),), required_suite_ids=("unit",), mode="fail-fast",
    )
    environment["LANG"] = "changed"
    assert plan.policy_fingerprint == "c" * 64
    assert plan.environment_digest == fingerprint({"LANG": "C"})
    assert plan.required_suite_ids == ("unit",)
    assert plan.mode == "fail-fast"


@pytest.mark.parametrize("provider_type", [LocalVerificationProvider, FakeVerificationProvider])
def test_fake_and_local_implement_the_same_execution_contract(tmp_path: Path, provider_type) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    plan = _plan("c" * 64, _suite())
    provider = provider_type(
        environment={"LANG": "C"},
        runner=lambda *args, **kwargs: ProcessResult(0, b"contract", b""),
        candidate_identity=lambda _root: (SHA, TREE),
    )
    receipt = provider.execute(plan, workspace=repository, run_id="contract-run")
    assert receipt.plan_fingerprint == plan.plan_fingerprint
    assert receipt.results[0].status == "PASS"
    assert receipt.result == "PASS"


def test_collect_all_records_partial_failure_and_fail_fast_skips(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    policy = "c" * 64
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return ProcessResult(7 if argv[-1] == "one" else 0, b"", b"")

    suites = (_suite("unit", argv=("tool", "one")), _suite("lint", argv=("tool", "two")))
    provider = _local(environment={"LANG": "C"}, runner=runner)
    collected = provider.execute(_plan(policy, *suites), workspace=repository)
    assert [item.status for item in collected.results] == ["FAIL", "PASS"]
    calls.clear()
    stopped = provider.execute(_plan(policy, *suites, mode="fail-fast"), workspace=repository)
    assert [item.status for item in stopped.results] == ["FAIL", "SKIPPED"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("raised", "error_code"),
    [(subprocess.TimeoutExpired("tool", 1), "timeout"), (FileNotFoundError("tool"), "process_unavailable")],
)
def test_timeout_and_process_unavailable_are_structured(tmp_path: Path, raised, error_code):
    repository = tmp_path / "repository"
    repository.mkdir()

    def runner(*args, **kwargs):
        raise raised

    receipt = _local(environment={"LANG": "C"}, runner=runner).execute(
        _plan("c" * 64, _suite()), workspace=repository,
    )
    assert receipt.result == "FAIL"
    assert receipt.results[0].error_code == error_code


def test_output_is_bounded_but_digest_covers_complete_bytes(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    output = b"0123456789"
    provider = _local(
        environment={"LANG": "C"}, output_limit=4,
        runner=lambda *args, **kwargs: ProcessResult(0, output, output),
    )
    result = provider.execute(_plan("c" * 64, _suite()), workspace=repository).results[0]
    assert result.stdout_preview == "0123"
    assert result.stdout_sha256 == __import__("hashlib").sha256(output).hexdigest()


def test_missing_and_oversize_artifact_fail_closed(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    provider = _local(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    )
    missing = _suite(artifacts=(ArtifactConstraint("missing.xml"),))
    assert provider.execute(_plan("c" * 64, missing), workspace=repository).results[0].error_code == "artifact_missing"
    (repository / "large.bin").write_bytes(b"12")
    large = _suite(artifacts=(ArtifactConstraint("large.bin", max_bytes=1),))
    assert provider.execute(_plan("c" * 64, large), workspace=repository).results[0].error_code == "artifact_too_large"


@pytest.mark.parametrize("name", ["PATH", "Path", "PYTHONPATH", "pythonhome"])
def test_path_and_pythonpath_injection_are_rejected(name: str):
    with pytest.raises(VerificationProviderError, match="PATH/PYTHONPATH"):
        _suite(environment_allowlist=(name,), environment={name: "evil"})


def test_policy_rejects_fake_network_reader_and_downgrade(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    attestor, _trust = _authority(tmp_path, repository)
    fake = _suite(requires_network=True, network_reader_id="fake-github-reader")
    with pytest.raises(VerificationProviderError) as failure:
        attestor.policy.authorize(_plan(attestor.policy.policy_fingerprint, fake), "key-1")
    assert failure.value.code == "network_unauthorized"
    with pytest.raises(VerificationProviderError) as downgrade:
        attestor.policy.authorize(_plan("d" * 64, _suite()), "key-1")
    assert downgrade.value.code == "policy_downgrade"


def test_key_policy_and_trust_store_must_be_outside_repository(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "policy.json").write_text("{}", encoding="utf-8")
    with pytest.raises(VerificationProviderError) as failure:
        ReceiptAttestor(
            repository=repository, policy_path=repository / "policy.json",
            private_key_path=repository / "missing", key_id="key-1", attestor_id="controller",
            context_reader=lambda _plan: ProtectedExecutionContext(SHA, TREE, "c" * 64),
        )
    assert failure.value.code == "untrusted_authority"
    with pytest.raises(VerificationProviderError) as offline:
        TrustStore.load(tmp_path / "offline.json", repository=repository)
    assert offline.value.code == "authority_unavailable"


def test_unsigned_self_signed_tamper_and_context_replay_are_rejected(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    attestor, trust = _authority(tmp_path, repository)
    plan = _plan(attestor.policy.policy_fingerprint, _suite())
    provider = _local(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    )
    receipt = provider.execute(plan, workspace=repository, run_id="run-1")
    signed = attestor.sign(receipt, plan)
    with pytest.raises(VerificationProviderError) as unsigned:
        trust.verify(
            replace(signed, signature=""), plan=plan, policy=attestor.policy,
            expected_run_id="run-1", expected_attempt=1,
        )
    assert unsigned.value.code == "unsigned_receipt"
    changed = dict(signed.payload)
    changed["result"] = "FAIL"
    with pytest.raises(VerificationProviderError) as tampered:
        trust.verify(replace(signed, payload=changed), plan=plan, policy=attestor.policy,
                     expected_run_id="run-1", expected_attempt=1)
    assert tampered.value.code == "invalid_signature"
    with pytest.raises(VerificationProviderError) as replay:
        trust.verify(
            signed, plan=plan, policy=attestor.policy,
            expected_run_id="other-run", expected_attempt=1,
        )
    assert replay.value.code == "context_replay"
    rogue = Ed25519PrivateKey.generate()
    rogue_envelope = replace(
        signed, key_id="rogue",
        signature=base64.b64encode(rogue.sign(json.dumps(
            signed.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode())).decode(),
    )
    with pytest.raises(VerificationProviderError) as self_signed:
        trust.verify(
            rogue_envelope, plan=plan, policy=attestor.policy,
            expected_run_id="run-1", expected_attempt=1,
        )
    assert self_signed.value.code == "untrusted_attestor"


def test_legacy_receipt_migration_is_explicit_and_preserves_source_fingerprint():
    old = VerificationReceiptEnvelope(
        "legacy-1", "plan-1", "REQ-020", "AID-172", SHA, TREE, {"os": "test"},
        "c" * 64,
        (VerificationCommandResult("unit", 0, "d" * 64, "e" * 64, 0.1),),
        "2026-09-06T00:00:00+00:00", "2026-09-06T00:00:01+00:00", "legacy", "1",
    )
    plan = VerificationPlan(
        "project", "REQ-020", 4, SHA, TREE, "f" * 64,
        fingerprint({"os": "test"}),
        (_suite("unit"),), ("unit",),
    )
    migrated = migrate_legacy_receipt(
        old, plan=plan, policy=AttestorPolicy(
            "policy", "f" * 64, ("key-1",), ("unit",), allow_legacy_migration=True,
        ), suite_types={"unit": "unit"},
    )
    assert migrated.legacy_receipt_fingerprint == fingerprint(old.to_dict())
    assert migrated.result == "PASS"
    with pytest.raises(VerificationProviderError):
        migrate_legacy_receipt(
            old, plan=plan, policy=AttestorPolicy(
                "policy", "f" * 64, ("key-1",), ("unit",), allow_legacy_migration=True,
            ), suite_types={},
        )


def test_required_suite_cannot_be_omitted():
    suite = _suite()
    with pytest.raises(VerificationProviderError) as failure:
        VerificationPlan(
            "project", "REQ-020", 4, SHA, TREE, "c" * 64,
            fingerprint({"LANG": "C"}), (suite,), ("security",),
        )
    assert failure.value.code == "missing_suite"


def test_environment_and_candidate_drift_are_rejected(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    plan = _plan("c" * 64, _suite())
    wrong_environment = _local(
        environment={"LANG": "other"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    )
    with pytest.raises(VerificationProviderError) as environment:
        wrong_environment.execute(plan, workspace=repository)
    assert environment.value.code == "environment_mismatch"
    stale = LocalVerificationProvider(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
        candidate_identity=lambda _root: ("9" * 40, TREE),
    )
    with pytest.raises(VerificationProviderError) as candidate:
        stale.execute(plan, workspace=repository)
    assert candidate.value.code == "stale_verification"


def test_local_provider_defaults_to_real_git_candidate_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "fixture"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "fixture@example.invalid"], check=True)
    (repository / "tracked.txt").write_text("candidate", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "candidate"], check=True,
                   capture_output=True)
    actual_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, capture_output=True,
        text=True,
    ).stdout.strip()
    actual_tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    plan = VerificationPlan(
        "project", "REQ-020", 4, actual_sha, actual_tree, "c" * 64,
        fingerprint({"LANG": "C"}), (_suite(),), ("unit",),
    )
    provider = LocalVerificationProvider(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    )
    assert provider.execute(plan, workspace=repository).result == "PASS"
    with pytest.raises(VerificationProviderError) as stale:
        provider.execute(replace(plan, candidate_sha="9" * 40), workspace=repository)
    assert stale.value.code == "stale_verification"


def test_attestor_rebuilds_context_and_requires_verified_network_reader(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    attestor, _trust = _authority(tmp_path, repository, network_readers=())
    suite = _suite(requires_network=True, network_reader_id="trusted-github-api")
    plan = _plan(attestor.policy.policy_fingerprint, suite)
    receipt = _local(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    ).execute(plan, workspace=repository)
    with pytest.raises(VerificationProviderError) as network:
        attestor.sign(receipt, plan)
    assert network.value.code == "network_unauthorized"
    attestor._context_reader = lambda _plan: (_ for _ in ()).throw(OSError("offline"))
    with pytest.raises(VerificationProviderError) as offline:
        attestor.sign(receipt, plan)
    assert offline.value.code == "attestor_offline"


def test_legacy_migration_policy_is_enforced_during_migration_and_signing(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    attestor, _trust = _authority(tmp_path, repository, allow_legacy=False)
    plan = _plan(attestor.policy.policy_fingerprint, _suite())
    old = VerificationReceiptEnvelope(
        "legacy", "old-plan", "REQ-020", "task", SHA, TREE, {"LANG": "C"}, "c" * 64,
        (VerificationCommandResult("unit", 0, "d" * 64, "e" * 64, 0.1),),
        "2026-09-06T00:00:00+00:00", "2026-09-06T00:00:01+00:00", "legacy", "1",
    )
    with pytest.raises(VerificationProviderError) as migration:
        migrate_legacy_receipt(old, plan=plan, policy=attestor.policy,
                               suite_types={"unit": "unit"})
    assert migration.value.code == "legacy_migration_forbidden"
    receipt = _local(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    ).execute(plan, workspace=repository)
    with pytest.raises(VerificationProviderError) as signing:
        attestor.sign(replace(receipt, legacy_receipt_fingerprint="f" * 64), plan)
    assert signing.value.code == "legacy_migration_forbidden"


def test_protected_headers_are_signed_versioned_and_attestor_keys_are_scoped(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    attestor, trust = _authority(tmp_path, repository)
    plan = _plan(attestor.policy.policy_fingerprint, _suite())
    receipt = _local(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    ).execute(plan, workspace=repository, run_id="run")
    signed = attestor.sign(receipt, plan)
    for changed in (
        replace(signed, schema_version=2),
        replace(signed, canonicalization="future-v2"),
        replace(signed, attestor_id="other"),
        replace(signed, key_id="other-key"),
    ):
        with pytest.raises(VerificationProviderError):
            trust.verify(changed, plan=plan, policy=attestor.policy,
                         expected_run_id="run", expected_attempt=1)
    unknown = signed.to_dict()
    unknown["downgrade"] = True
    with pytest.raises(VerificationProviderError) as header:
        SignedReceiptEnvelope.from_dict(unknown)
    assert header.value.code == "unknown_header"


def _resign(attestor, envelope, payload):
    changed = replace(envelope, payload=payload, signature="")
    signature = base64.b64encode(attestor._key.sign(_canonical({
        "protected": changed.protected(), "payload": payload,
    }))).decode("ascii")
    return replace(changed, signature=signature)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {**payload, "unknown": True},
        lambda payload: {key: value for key, value in payload.items() if key != "provider_id"},
        lambda payload: {**payload, "schema_version": 999},
        lambda payload: {**payload, "phase": "4"},
        lambda payload: {**payload, "results": "PASS"},
        lambda payload: {**payload, "results": [{**payload["results"][0], "returncode": "0"}]},
    ],
)
def test_trust_store_strictly_rejects_malformed_signed_receipt_payload(tmp_path: Path,
                                                                      mutation) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    attestor, trust = _authority(tmp_path, repository)
    plan = _plan(attestor.policy.policy_fingerprint, _suite())
    receipt = _local(
        environment={"LANG": "C"}, runner=lambda *args, **kwargs: ProcessResult(0, b"", b""),
    ).execute(plan, workspace=repository, run_id="run")
    signed = attestor.sign(receipt, plan)
    malformed = _resign(attestor, signed, mutation(dict(signed.payload)))
    with pytest.raises(VerificationProviderError):
        trust.verify(malformed, plan=plan, policy=attestor.policy,
                     expected_run_id="run", expected_attempt=1)


def test_rfc8785_canonicalization_official_number_and_unicode_vectors() -> None:
    assert _canonical({"numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27]}) == (
        b'{"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27]}'
    )
    assert _canonical({"\u20ac": "Euro", "a": "ASCII", "\u00e9": "Latin"}) == (
        '{"a":"ASCII","é":"Latin","€":"Euro"}'.encode()
    )
    with pytest.raises(VerificationProviderError):
        _canonical({"invalid": float("nan")})
    with pytest.raises(VerificationProviderError):
        _canonical({"unsafe_integer": 9007199254740992})
