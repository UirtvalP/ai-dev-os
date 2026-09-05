from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path

import pytest

from workspace_orchestrator import workspace as workspace_module
from workspace_orchestrator.adapters.base import TaskProviderError
from workspace_orchestrator.adapters.git import LocalGitProvider
from workspace_orchestrator.cli import build_parser
from workspace_orchestrator.models import Task
from workspace_orchestrator.phase_gate import (
    AcceptanceResult,
    GateStore,
    PhaseActivationRecord,
    PhaseGateError,
    PhaseGateRecord,
    PhaseTransitionGuard,
    ReviewAttestation,
    VerificationReceipt,
    content_fingerprint,
    gate_record_fingerprint,
    source_fingerprint,
    verification_receipt_fingerprint,
)
from workspace_orchestrator.workspace import WORKSPACE_FILES, WorkspaceStore

SHA_0 = "0" * 40
SHA_1 = "1" * 40
PLAN_0_SOURCE = "# Phase 0\n\nAudit and exact-SHA gate.\n"
ACCEPTANCE_0 = (
    {"id": "P0-AC-06", "description": "CI and type checking pass"},
    {"id": "P0-AC-07", "description": "exact-SHA phase gate"},
)
RECEIPT_CI = "receipt-ci"
RECEIPT_GATE = "receipt-gate"
QUALITY_SUITE = {
    "id": "quality",
    "kind": "command",
    "commands": [["python", "-m", "pytest", "-q"]],
}
QUALITY_SUITE_FINGERPRINT = content_fingerprint(QUALITY_SUITE)
QUALITY_COMMAND = '[["python","-m","pytest","-q"]]'
_DEFAULT_NEXT = object()


@dataclass
class FakeGit:
    head: str = SHA_0
    ancestors: set[tuple[str, str]] | None = None
    clean: bool = True
    files: dict[tuple[str, str], str] | None = None

    def head_sha(self) -> str:
        return self.head

    def is_clean(self) -> bool:
        return self.clean

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        return (ancestor_sha, descendant_sha) in (self.ancestors or set())

    def read_file_at(self, revision: str, relative_path: str) -> str:
        selected_revision = self.head if revision == "HEAD" else revision
        try:
            return (self.files or {})[(selected_revision, relative_path)]
        except KeyError as exc:
            raise RuntimeError(f"missing {selected_revision}:{relative_path}") from exc

    def list_files_at(self, revision: str, prefix: str) -> tuple[str, ...]:
        selected_revision = self.head if revision == "HEAD" else revision
        return tuple(
            sorted(
                path
                for (sha, path) in (self.files or {})
                if sha == selected_revision and path.startswith(prefix)
            )
        )


def _install_definition(
    git: FakeGit,
    *,
    phase: int = 0,
    sha: str = SHA_0,
    task_id: str | None = None,
    plan_source: str = PLAN_0_SOURCE,
    acceptance: tuple[dict[str, str], ...] = ACCEPTANCE_0,
    next_task_id: str | None | object = _DEFAULT_NEXT,
) -> None:
    files = git.files if git.files is not None else {}
    git.files = files
    if phase > 0:
        for previous_phase in range(phase):
            previous_path = GateStore.definition_path("REQ-001", previous_phase)
            if (sha, previous_path) not in files:
                _install_definition(
                    git,
                    phase=previous_phase,
                    sha=sha,
                    next_task_id=f"AID-{previous_phase + 2}",
                )
    effective_next = (
        ("AID-2" if phase == 0 else None)
        if next_task_id is _DEFAULT_NEXT
        else next_task_id
    )
    source_path = f"plans/phase-{phase}.md"
    definition_path = GateStore.definition_path("REQ-001", phase)
    files[(sha, source_path)] = plan_source
    files[(sha, definition_path)] = json.dumps(
        {
            "schema_version": 1,
            "requirement_id": "REQ-001",
            "phase": phase,
            "task_id": task_id or f"AID-{phase + 1}",
            "plan_source_path": source_path,
            "plan_source_fingerprint": source_fingerprint(plan_source),
            "acceptance": list(acceptance),
            "verification_suites": [QUALITY_SUITE],
            "next_task_id": effective_next,
        }
    )


@dataclass
class FakeTasks:
    tasks: dict[str, Task]
    updates: list[tuple[str, str]]
    links: list[tuple[str, str]]
    unlinks: list[tuple[str, str]]

    def get_task(self, task_id: str) -> Task:
        return self.tasks[task_id]

    def update_status(self, task_id: str, status: str) -> Task:
        self.updates.append((task_id, status))
        task = self.tasks[task_id]
        updated = replace(task, status=status, version=(task.version or 0) + 1)
        self.tasks[task_id] = updated
        return updated

    def compare_and_set_status(
        self,
        task_id: str,
        *,
        expected_version: int,
        expected_status: str,
        status: str,
    ) -> Task:
        current = self.tasks[task_id]
        if current.version != expected_version or current.status != expected_status:
            raise TaskProviderError("stale task")
        return self.update_status(task_id, status)

    def link_session(self, task_id: str, session_id: str, **_: object) -> None:
        self.links.append((task_id, session_id))

    def unlink_session(self, task_id: str, session_id: str) -> None:
        self.unlinks.append((task_id, session_id))


def _review(
    *,
    reviewer: str = "review-session",
    implementers: tuple[str, ...] = ("implementation-session",),
    run_ids: tuple[str, ...] = ("run-ci", "run-gate"),
    verdict: str = "PASS",
    reviewed_at: str = "2026-09-05T12:00:00+08:00",
) -> ReviewAttestation:
    return ReviewAttestation(
        reviewer_session_id=reviewer,
        implementation_session_ids=implementers,
        implementation_run_ids=run_ids,
        verdict=verdict,
        resolved_findings=("finding-001",),
        reviewed_at=reviewed_at,
    )


def _record(
    *,
    phase: int = 0,
    sha: str = SHA_0,
    plan_source: str = PLAN_0_SOURCE,
    acceptance: object = ACCEPTANCE_0,
    results: tuple[AcceptanceResult, ...] | None = None,
    review: ReviewAttestation | None = None,
    status: str = "PASS",
    previous: PhaseGateRecord | None = None,
) -> PhaseGateRecord:
    receipt_ids = (
        (RECEIPT_CI, RECEIPT_GATE)
        if phase == 0
        else (f"receipt-phase-{phase}",)
    )
    evidence = receipt_ids[0]
    receipt_values = tuple(
        _receipt(
            receipt_id,
            sha=sha,
            run_id=(
                ("run-ci", "run-gate")[index]
                if phase == 0
                else f"run-phase-{phase}"
            ),
            started_at=(
                "2026-09-05T10:00:00+08:00"
                if phase == 0
                else "2026-09-05T12:32:00+08:00"
            ),
            completed_at=(
                "2026-09-05T11:00:00+08:00"
                if phase == 0
                else "2026-09-05T12:40:00+08:00"
            ),
        )
        for index, receipt_id in enumerate(receipt_ids)
    )
    return PhaseGateRecord(
        requirement_id="REQ-001",
        phase=phase,
        task_id=f"AID-{phase + 1}",
        commit_sha=sha,
        previous_gate_phase=previous.phase if previous else None,
        previous_gate_commit_sha=previous.commit_sha if previous else None,
        previous_gate_record_fingerprint=(
            gate_record_fingerprint(previous) if previous else None
        ),
        plan_fingerprint=source_fingerprint(plan_source),
        acceptance_fingerprint=content_fingerprint(acceptance),
        acceptance_results=results
        or (
            AcceptanceResult("P0-AC-06", "PASS", "CI pass", (evidence,)),
            AcceptanceResult("P0-AC-07", "PASS", "Gate pass", (evidence,)),
        ),
        verification_receipt_refs=receipt_ids,
        verification_receipt_fingerprints=tuple(
            verification_receipt_fingerprint(receipt) for receipt in receipt_values
        ),
        regression_summary="All V1 and V2 tests passed.",
        review_attestation=review
        or _review(
            run_ids=(
                ("run-ci", "run-gate")
                if phase == 0
                else (f"run-phase-{phase}",)
            ),
            reviewed_at=(
                "2026-09-05T12:00:00+08:00"
                if phase == 0
                else "2026-09-05T12:50:00+08:00"
            ),
        ),
        issued_at=(
            "2026-09-05T12:30:00+08:00"
            if phase == 0
            else "2026-09-05T13:00:00+08:00"
        ),
        issued_by="phase-gate-test",
        status=status,
    )


def _receipt(
    receipt_id: str,
    *,
    sha: str,
    run_id: str,
    session_id: str = "implementation-session",
    started_at: str = "2026-09-05T10:00:00+08:00",
    completed_at: str = "2026-09-05T11:00:00+08:00",
) -> VerificationReceipt:
    return VerificationReceipt(
        receipt_id=receipt_id,
        requirement_id="REQ-001",
        commit_sha=sha,
        suite_id="quality",
        suite_fingerprint=QUALITY_SUITE_FINGERPRINT,
        issuer="workspace-command-runner",
        run_id=run_id,
        session_id=session_id,
        command=QUALITY_COMMAND,
        environment="test-os / Python test",
        started_at=started_at,
        completed_at=completed_at,
        exit_code=0,
        status="PASS",
        summary="tests passed",
    )


def _install_phase_receipts(
    gates: GateStore,
    *,
    phase: int,
    sha: str,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> None:
    if phase == 0:
        gates._write_verification_receipt(
            _receipt(RECEIPT_CI, sha=sha, run_id="run-ci")
        )
        gates._write_verification_receipt(
            _receipt(RECEIPT_GATE, sha=sha, run_id="run-gate")
        )
        return
    gates._write_verification_receipt(
        _receipt(
            f"receipt-phase-{phase}",
            sha=sha,
            run_id=f"run-phase-{phase}",
            started_at=started_at or "2026-09-05T10:00:00+08:00",
            completed_at=completed_at or "2026-09-05T11:00:00+08:00",
        )
    )


def _install_activation(
    gates: GateStore,
    previous: PhaseGateRecord,
    *,
    activated_at: str = "2026-09-05T12:31:00+08:00",
) -> None:
    gates.workspace_store.touch_meta("REQ-001", requirement_task_id="AID-2")
    gates._write_activation(
        PhaseActivationRecord(
            requirement_id="REQ-001",
            phase=1,
            task_id="AID-2",
            predecessor_gate_phase=0,
            predecessor_gate_commit_sha=previous.commit_sha,
            predecessor_gate_record_fingerprint=gate_record_fingerprint(previous),
            session_id="session-1",
            activated_at=activated_at,
            activated_by="codex",
        )
    )


@pytest.fixture
def gate_store(tmp_path: Path) -> tuple[WorkspaceStore, FakeGit, GateStore]:
    workspace = WorkspaceStore(tmp_path)
    assert workspace.create("Phase gate", task_provider=None) == "REQ-001"
    workspace.touch_meta(
        "REQ-001", phase_gate_required=True, requirement_task_id="AID-1"
    )
    git = FakeGit()
    _install_definition(git)
    _install_definition(
        git, phase=1, sha=SHA_0, task_id="AID-2", next_task_id=None
    )
    gates = GateStore(workspace, git)
    _install_phase_receipts(gates, phase=0, sha=SHA_0)
    return workspace, git, gates


def test_phase_zero_gate_is_written_atomically_to_optional_workspace_directory(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    record = _record()

    path = gates._write_gate_record(record)

    assert path == workspace.path_for("REQ-001") / "phase-gates" / "phase-0.json"
    assert gates.read("REQ-001", 0) == record
    assert "phase-gates" not in WORKSPACE_FILES
    assert workspace.load("REQ-001")["meta"]["id"] == "REQ-001"


def test_gate_store_raw_writer_helpers_use_internal_names(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store

    assert not hasattr(gates, "write")
    assert not hasattr(gates, "issue")
    assert not hasattr(gates, "write_activation")
    assert not hasattr(gates, "write_verification_receipt")


def test_issue_derives_head_fingerprints_and_predecessor_reference(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = gates._issue_validated(
        requirement_id="req-001",
        phase=0,
        acceptance_results=(
            AcceptanceResult("P0-AC-06", "PASS", "pass", (RECEIPT_CI,)),
            AcceptanceResult("P0-AC-07", "PASS", "pass", (RECEIPT_GATE,)),
        ),
        verification_receipt_refs=(RECEIPT_CI, RECEIPT_GATE),
        regression_summary="regression pass",
        review_attestation=_review(),
        issued_at="2026-09-05T12:00:00+08:00",
        issued_by="gate-issuer",
    )
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_phase_receipts(
        gates,
        phase=1,
        sha=SHA_1,
        started_at="2026-09-05T12:32:00+08:00",
        completed_at="2026-09-05T12:40:00+08:00",
    )
    acceptance_one = ({"id": "P1-AC-01", "description": "runtime contract"},)
    _install_definition(
        git,
        phase=1,
        sha=SHA_1,
        plan_source="# Phase 1\n",
        acceptance=acceptance_one,
    )
    _install_activation(gates, phase_zero)
    phase_one = gates._issue_validated(
        requirement_id="REQ-001",
        phase=1,
        acceptance_results=(
            AcceptanceResult("P1-AC-01", "PASS", "pass", ("receipt-phase-1",)),
        ),
        verification_receipt_refs=("receipt-phase-1",),
        regression_summary="regression pass",
        review_attestation=_review(
            run_ids=("run-phase-1",),
            reviewed_at="2026-09-05T12:50:00+08:00",
        ),
        issued_at="2026-09-05T13:00:00+08:00",
        issued_by="gate-issuer",
    )

    assert phase_zero.commit_sha == SHA_0
    assert phase_zero.plan_fingerprint == source_fingerprint(PLAN_0_SOURCE)
    assert phase_one.previous_gate_phase == 0
    assert phase_one.previous_gate_commit_sha == SHA_0
    assert phase_one.previous_gate_record_fingerprint == gate_record_fingerprint(phase_zero)


def test_production_issue_uses_create_once_review_and_runtime_issuer(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, gates = gate_store
    from workspace_orchestrator.phase_verification import PhaseVerificationRunner

    revalidated: list[str] = []

    def revalidate(
        _self: PhaseVerificationRunner,
        _requirement_id: str,
        *,
        phase: int,
        receipt: VerificationReceipt,
    ) -> None:
        assert phase == 0
        revalidated.append(receipt.receipt_id)

    monkeypatch.setattr(PhaseVerificationRunner, "revalidate", revalidate)
    review = gates.record_review_from_payload(
        "REQ-001",
        0,
        {
            "verification_receipt_refs": [RECEIPT_CI, RECEIPT_GATE],
            "implementation_session_ids": ["implementation-session"],
            "implementation_run_ids": ["run-ci", "run-gate"],
            "verdict": "PASS",
            "resolved_findings": ["all blockers resolved"],
        },
        reviewer_session_id="independent-reviewer",
    )

    record = gates.issue_from_payload(
        "REQ-001",
        0,
        {
            "acceptance_results": [
                AcceptanceResult(
                    "P0-AC-06", "PASS", "CI pass", (RECEIPT_CI,)
                ).to_dict(),
                AcceptanceResult(
                    "P0-AC-07", "PASS", "Gate pass", (RECEIPT_GATE,)
                ).to_dict(),
            ],
            "regression_summary": "all regression tests passed",
        },
        issued_by="codex:issuer-session",
    )

    assert record.review_attestation == review.attestation
    assert record.verification_receipt_refs == review.verification_receipt_refs
    assert record.issued_by == "codex:issuer-session"
    assert record.issued_at >= review.attestation.reviewed_at
    assert revalidated == [RECEIPT_CI, RECEIPT_GATE]


def test_production_issue_packet_cannot_self_report_review_receipts_or_time(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match="不得自报"):
        gates.issue_from_payload(
            "REQ-001",
            0,
            {
                "acceptance_results": [],
                "verification_receipt_refs": [RECEIPT_CI],
                "review_attestation": _review().to_dict(),
                "issued_at": "2026-09-05T12:30:00+08:00",
                "issued_by": "forged",
                "regression_summary": "forged",
            },
            issued_by="codex:issuer-session",
        )


def test_production_issue_rejects_receipt_changed_after_independent_review(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    gates.record_review_from_payload(
        "REQ-001",
        0,
        {
            "verification_receipt_refs": [RECEIPT_CI, RECEIPT_GATE],
            "implementation_session_ids": ["implementation-session"],
            "implementation_run_ids": ["run-ci", "run-gate"],
            "verdict": "PASS",
            "resolved_findings": ["all blockers resolved"],
        },
        reviewer_session_id="independent-reviewer",
    )
    receipt_path = gates.receipt_path_for("REQ-001", RECEIPT_CI)
    tampered = gates.read_verification_receipt("REQ-001", RECEIPT_CI).to_dict()
    tampered["summary"] = "changed after review"
    workspace.write_json(receipt_path, tampered)

    with pytest.raises(PhaseGateError, match="Review 后 Verification Receipt 内容已变化"):
        gates.issue_from_payload(
            "REQ-001",
            0,
            {
                "acceptance_results": [
                    AcceptanceResult(
                        "P0-AC-06", "PASS", "CI pass", (RECEIPT_CI,)
                    ).to_dict(),
                    AcceptanceResult(
                        "P0-AC-07", "PASS", "Gate pass", (RECEIPT_GATE,)
                    ).to_dict(),
                ],
                "regression_summary": "all regression tests passed",
            },
            issued_by="codex:issuer-session",
        )


def test_independent_review_cannot_omit_reviewer_implementation_receipt(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    receipt_path = gates.receipt_path_for("REQ-001", RECEIPT_CI)
    reviewer_receipt = _receipt(
        RECEIPT_CI,
        sha=SHA_0,
        run_id="run-ci",
        session_id="independent-reviewer",
    )
    workspace.write_json(receipt_path, reviewer_receipt.to_dict())

    with pytest.raises(PhaseGateError, match="Reviewer Session 参与"):
        gates.record_review_from_payload(
            "REQ-001",
            0,
            {
                "verification_receipt_refs": [RECEIPT_CI, RECEIPT_GATE],
                "implementation_session_ids": ["implementation-session"],
                "implementation_run_ids": ["run-ci", "run-gate"],
                "verdict": "PASS",
                "resolved_findings": [],
            },
            reviewer_session_id="independent-reviewer",
        )


def test_phase_gate_models_are_immutable() -> None:
    acceptance = AcceptanceResult("P0-AC-07", "PASS", "pass")
    review = _review()
    record = _record(results=(acceptance,), review=review)

    with pytest.raises(FrozenInstanceError):
        acceptance.status = "FAIL"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        review.verdict = "FAIL"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        record.commit_sha = SHA_1  # type: ignore[misc]


def test_future_schema_and_unknown_json_fields_round_trip_without_losing_metadata() -> None:
    payload = _record().to_dict()
    payload["schema_version"] = 2
    payload["future_gate_metadata"] = {"trace": [1, 2, 3]}
    raw_results = payload["acceptance_results"]
    assert isinstance(raw_results, list)
    first_result = raw_results[0]
    assert isinstance(first_result, dict)
    first_result["future_result_metadata"] = "kept"
    raw_review = payload["review_attestation"]
    assert isinstance(raw_review, dict)
    raw_review["future_review_metadata"] = True

    loaded = PhaseGateRecord.from_dict(payload)
    round_tripped = loaded.to_dict()

    assert round_tripped["schema_version"] == 2
    assert round_tripped["future_gate_metadata"] == {"trace": [1, 2, 3]}
    assert round_tripped["acceptance_results"][0]["future_result_metadata"] == "kept"  # type: ignore[index]
    assert round_tripped["review_attestation"]["future_review_metadata"] is True  # type: ignore[index]


def test_future_schema_round_trips_but_cannot_authorize_transition(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    payload = _record().to_dict()
    payload["schema_version"] = 2
    payload["future_authority"] = {"revoked": True}
    loaded = PhaseGateRecord.from_dict(payload)

    with pytest.raises(PhaseGateError, match="不支持 PhaseGateRecord schema_version=2"):
        gates.validate(loaded)


def test_canonical_fingerprint_is_independent_of_mapping_order() -> None:
    assert content_fingerprint({"b": 2, "a": 1}) == content_fingerprint({"a": 1, "b": 2})


def test_non_json_fingerprint_input_fails_closed() -> None:
    with pytest.raises(PhaseGateError, match="非 JSON"):
        content_fingerprint({object()})


@pytest.mark.parametrize("sha", ["a" * 39, "A" * 40, "g" * 40])
def test_gate_rejects_non_exact_lowercase_40_character_sha(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore], sha: str
) -> None:
    _, git, gates = gate_store
    git.head = sha

    with pytest.raises(PhaseGateError, match="40 位"):
        gates._write_gate_record(_record(sha=sha))


def test_gate_rejects_record_for_stale_head(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    git.head = SHA_1

    with pytest.raises(PhaseGateError, match="SHA 已陈旧"):
        gates._write_gate_record(_record())


def test_gate_rejects_dirty_worktree(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    git.clean = False

    with pytest.raises(PhaseGateError, match="未提交变更"):
        gates._write_gate_record(_record())


@pytest.mark.parametrize("changed_source", ["plan", "acceptance"])
def test_plan_or_acceptance_change_invalidates_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    changed_source: str,
) -> None:
    _, git, gates = gate_store
    if changed_source == "plan":
        _install_definition(git, plan_source="# Changed plan\n")
        message = "计划内容已变化"
    else:
        changed_acceptance = (
            {"id": "P0-AC-06", "description": "changed CI definition"},
            {"id": "P0-AC-07", "description": "changed gate definition"},
        )
        _install_definition(git, acceptance=changed_acceptance)
        message = "验收定义已变化"

    with pytest.raises(PhaseGateError, match=message):
        gates._write_gate_record(_record())


def test_duplicate_acceptance_ids_are_rejected(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    results = (
        AcceptanceResult("P0-AC-07", "PASS", "first"),
        AcceptanceResult("P0-AC-07", "PASS", "duplicate"),
    )

    with pytest.raises(PhaseGateError, match="Acceptance ID 重复"):
        gates._write_gate_record(_record(results=results))


def test_any_failed_acceptance_rejects_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    results = (AcceptanceResult("P0-AC-07", "FAIL", "not ready"),)

    with pytest.raises(PhaseGateError, match="未全部 PASS"):
        gates._write_gate_record(_record(results=results))


@pytest.mark.parametrize(
    "results",
    [
        (AcceptanceResult("P0-AC-07", "PASS", "pass", (RECEIPT_GATE,)),),
        (
            AcceptanceResult("P0-AC-06", "PASS", "pass", (RECEIPT_CI,)),
            AcceptanceResult("P0-AC-07", "PASS", "pass", (RECEIPT_GATE,)),
            AcceptanceResult("P0-AC-99", "PASS", "extra", (RECEIPT_GATE,)),
        ),
    ],
)
def test_acceptance_results_must_exactly_cover_definition_ids(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    results: tuple[AcceptanceResult, ...],
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match="精确覆盖"):
        gates._write_gate_record(_record(results=results))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"verification_receipt_refs": ()}, "Verification Receipt"),
        (
            {
                "results": (
                    AcceptanceResult("P0-AC-06", "PASS", "pass", ()),
                    AcceptanceResult("P0-AC-07", "PASS", "pass", (RECEIPT_GATE,)),
                )
            },
            "引用至少一项证据",
        ),
        ({"issued_at": "2026-09-05T12:00:00"}, "issued_at 必须包含时区"),
        (
            {"review": replace(_review(), reviewed_at="2026-09-05T12:00:00")},
            "reviewed_at 必须包含时区",
        ),
    ],
)
def test_pass_gate_requires_receipts_evidence_and_zoned_timestamps(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    change: dict[str, object],
    message: str,
) -> None:
    _, _, gates = gate_store
    record = _record(
        results=change.get("results") if isinstance(change.get("results"), tuple) else None,
        review=(
            change.get("review")
            if isinstance(change.get("review"), ReviewAttestation)
            else None
        ),
    )
    if "verification_receipt_refs" in change:
        record = replace(record, verification_receipt_refs=())
    if "issued_at" in change:
        record = replace(record, issued_at=str(change["issued_at"]))

    with pytest.raises(PhaseGateError, match=message):
        gates._write_gate_record(record)


def test_gate_rejects_nonexistent_receipt_even_when_caller_declares_it(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    record = replace(
        _record(),
        acceptance_results=(
            AcceptanceResult("P0-AC-06", "PASS", "pass", ("missing-receipt",)),
            AcceptanceResult("P0-AC-07", "PASS", "pass", ("missing-receipt",)),
        ),
        verification_receipt_refs=("missing-receipt",),
        verification_receipt_fingerprints=("0" * 64,),
        review_attestation=_review(run_ids=("missing-run",)),
    )

    with pytest.raises(PhaseGateError, match="缺少 Verification Receipt"):
        gates._write_gate_record(record)


def test_gate_rejects_receipt_bound_to_another_commit(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    gates._write_verification_receipt(
        _receipt("wrong-sha", sha=SHA_1, run_id="run-wrong")
    )
    record = replace(
        _record(),
        acceptance_results=(
            AcceptanceResult("P0-AC-06", "PASS", "pass", ("wrong-sha",)),
            AcceptanceResult("P0-AC-07", "PASS", "pass", ("wrong-sha",)),
        ),
        verification_receipt_refs=("wrong-sha",),
        verification_receipt_fingerprints=(
            verification_receipt_fingerprint(
                _receipt("wrong-sha", sha=SHA_1, run_id="run-wrong")
            ),
        ),
        review_attestation=_review(run_ids=("run-wrong",)),
    )

    with pytest.raises(PhaseGateError, match="exact SHA"):
        gates._write_gate_record(record)


@pytest.mark.parametrize(
    ("review", "message"),
    [
        (
            replace(_review(), implementation_session_ids=(" ",)),
            "Session ID 不能为空",
        ),
        (replace(_review(), implementation_run_ids=(" ",)), "Run ID 不能为空"),
        (
            replace(_review(), reviewed_at="2026-09-05T13:00:00+08:00"),
            "不能晚于 Gate issued_at",
        ),
    ],
)
def test_review_rejects_blank_provenance_and_future_timestamp(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    review: ReviewAttestation,
    message: str,
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match=message):
        gates._write_gate_record(_record(review=review))


def test_verification_receipt_is_create_once_and_same_content_idempotent(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    receipt = _receipt("receipt-extra", sha=SHA_0, run_id="run-extra")

    path = gates._write_verification_receipt(receipt)

    assert gates._write_verification_receipt(receipt) == path
    with pytest.raises(PhaseGateError, match="不可覆盖"):
        gates._write_verification_receipt(replace(receipt, summary="changed"))


def test_gate_rejects_receipt_content_tampered_after_issue(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    record = _record()
    gates._write_gate_record(record)
    path = gates.receipt_path_for("REQ-001", RECEIPT_CI)
    payload = workspace.read_json(path)
    payload["summary"] = "forged after gate issue"
    workspace.write_json(path, payload)

    with pytest.raises(PhaseGateError, match="内容指纹已变化"):
        gates.validate(record)


def test_all_receipts_must_complete_before_independent_review(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    late = replace(
        _receipt("late", sha=SHA_0, run_id="run-late"),
        completed_at="2026-09-05T12:15:00+08:00",
    )
    gates._write_verification_receipt(late)
    record = replace(
        _record(),
        acceptance_results=(
            AcceptanceResult("P0-AC-06", "PASS", "pass", ("late",)),
            AcceptanceResult("P0-AC-07", "PASS", "pass", ("late",)),
        ),
        verification_receipt_refs=("late",),
        verification_receipt_fingerprints=(
            verification_receipt_fingerprint(late),
        ),
        review_attestation=_review(run_ids=("run-late",)),
    )

    with pytest.raises(PhaseGateError, match="晚于独立 Review"):
        gates._write_gate_record(record)


def test_future_gate_issue_time_is_rejected(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match="未来时间"):
        gates._write_gate_record(replace(_record(), issued_at="2999-01-01T00:00:00+00:00"))


@pytest.mark.parametrize(
    ("review", "message"),
    [
        (_review(reviewer="same", implementers=("same",)), "不能参与"),
        (_review(verdict="FAIL"), "Review 未通过"),
        (replace(_review(), implementation_session_ids=()), "至少需要一个实现 Session"),
        (replace(_review(), implementation_run_ids=()), "至少需要一个实现 Run ID"),
    ],
)
def test_review_must_pass_and_be_independent(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    review: ReviewAttestation,
    message: str,
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match=message):
        gates._write_gate_record(_record(review=review))


def test_gate_status_must_be_pass(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match="Phase Gate 未通过"):
        gates._write_gate_record(_record(status="FAIL"))


def test_failed_rewrite_does_not_damage_existing_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _, gates = gate_store
    path = gates.path_for("REQ-001", 0)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(workspace_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        gates._write_gate_record(_record())

    assert not path.exists()
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))
    assert workspace.load("REQ-001")["meta"]["id"] == "REQ-001"


def test_same_gate_write_is_idempotent_but_different_record_cannot_overwrite(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    record = _record()
    path = gates._write_gate_record(record)
    before = path.read_bytes()

    assert gates._write_gate_record(record) == path
    with pytest.raises(PhaseGateError, match="不可覆盖"):
        gates._write_gate_record(replace(record, issued_at="2026-09-05T13:00:00+08:00"))
    assert path.read_bytes() == before


def test_transition_guard_advances_only_after_valid_exact_sha_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    _install_definition(
        gates.git,  # type: ignore[arg-type]
        phase=1,
        sha=SHA_0,
        task_id="AID-2",
        next_task_id=None,
    )
    gates._write_gate_record(_record())
    workspace.write_json(
        workspace.path_for("REQ-001") / "sessions.json",
        [
            {
                "id": "session-1",
                "agent": "codex",
                "task_ids": ["AID-1"],
                "result": "in_progress",
            }
        ],
    )
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]

    advanced = guard.advance_next(
        "REQ-001",
        completed_phase=0,
        session_id="session-1",
        activated_by="codex",
    )

    assert advanced.status == "in_progress"
    assert advanced.version == 5
    assert tasks.updates == [("AID-1", "done"), ("AID-2", "in_progress")]
    assert workspace.load("REQ-001")["meta"]["requirement_task_id"] == "AID-2"
    assert workspace.load("REQ-001")["sessions"][0]["task_ids"] == ["AID-2"]
    assert tasks.links == [("AID-2", "session-1")]
    assert tasks.unlinks == [("AID-1", "session-1")]
    activation = gates.read_activation("REQ-001", 1)
    assert activation.task_id == "AID-2"
    assert gates.require_task_active("REQ-001", "AID-2") == "activated"

    repeated = guard.advance_next(
        "REQ-001",
        completed_phase=0,
        session_id="session-1",
        activated_by="codex",
    )
    assert repeated == advanced
    assert tasks.updates == [("AID-1", "done"), ("AID-2", "in_progress")]


def test_transition_retries_after_provider_failure_without_reopening_completed_task(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    gates._write_gate_record(_record())
    workspace.write_json(
        workspace.path_for("REQ-001") / "sessions.json",
        [{"id": "session-1", "task_ids": ["AID-1"], "result": "in_progress"}],
    )

    class FailOnceTasks(FakeTasks):
        failed = False

        def compare_and_set_status(
            self,
            task_id: str,
            *,
            expected_version: int,
            expected_status: str,
            status: str,
        ) -> Task:
            if task_id == "AID-2" and status == "in_progress" and not self.failed:
                self.failed = True
                raise TaskProviderError("transient")
            return super().compare_and_set_status(
                task_id,
                expected_version=expected_version,
                expected_status=expected_status,
                status=status,
            )

    tasks = FailOnceTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]

    with pytest.raises(PhaseGateError, match="transient"):
        guard.advance_next(
            "REQ-001",
            completed_phase=0,
            session_id="session-1",
            activated_by="codex",
        )

    assert tasks.tasks["AID-1"].status == "done"
    assert tasks.tasks["AID-2"].status == "blocked"
    assert gates.read_activation("REQ-001", 1).task_id == "AID-2"

    recovered = guard.advance_next(
        "REQ-001",
        completed_phase=0,
        session_id="session-1",
        activated_by="codex",
    )
    assert recovered.status == "in_progress"
    assert tasks.updates.count(("AID-1", "done")) == 1


def test_stale_transition_retry_never_rolls_back_active_next_phase(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, git, gates = gate_store
    gates._write_gate_record(_record())
    workspace.write_json(
        workspace.path_for("REQ-001") / "sessions.json",
        [{"id": "session-1", "task_ids": ["AID-1"], "result": "in_progress"}],
    )
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]
    guard.advance_next(
        "REQ-001", completed_phase=0, session_id="session-1", activated_by="codex"
    )
    updates = list(tasks.updates)
    git.head = SHA_1
    git.clean = False
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, sha=SHA_1)
    _install_definition(
        git, phase=1, sha=SHA_1, task_id="AID-2", next_task_id=None
    )

    repeated = guard.advance_next(
        "REQ-001", completed_phase=0, session_id="session-1", activated_by="codex"
    )

    assert repeated.status == "in_progress"
    assert tasks.tasks["AID-2"].status == "in_progress"
    assert tasks.updates == updates


@pytest.mark.parametrize(
    "crash_boundary",
    (
        "after_activation",
        "after_meta",
        "after_session_rebind",
        "after_completed_task",
        "after_next_task",
    ),
)
def test_write_ahead_activation_allows_new_session_to_resume_partial_transition(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
    crash_boundary: str,
) -> None:
    from workspace_orchestrator.automation import session_runtime as session_runtime_module
    from workspace_orchestrator.automation.session_runtime import (
        attach_session,
        end_session,
    )

    workspace, _, gates = gate_store
    gates._write_gate_record(_record())
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )
    attach_session(
        workspace,
        "REQ-001",
        session_id="session-1",
        agent_name="codex",
        task_provider=tasks,  # type: ignore[arg-type]
        task_ids=("AID-1",),
    )
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]
    crashed = False

    if crash_boundary in {"after_activation", "after_meta"}:
        original_meta_cas = WorkspaceStore.compare_and_set_meta

        def crash_around_meta(
            store: WorkspaceStore, *args: object, **kwargs: object
        ) -> object:
            nonlocal crashed
            if store is workspace and not crashed and crash_boundary == "after_activation":
                crashed = True
                raise RuntimeError("simulated crash after activation")
            result = original_meta_cas(store, *args, **kwargs)  # type: ignore[arg-type]
            if store is workspace and not crashed:
                crashed = True
                raise RuntimeError("simulated crash after meta")
            return result

        monkeypatch.setattr(WorkspaceStore, "compare_and_set_meta", crash_around_meta)
    elif crash_boundary == "after_session_rebind":
        original_rebind = session_runtime_module.rebind_session_task

        def crash_after_rebind(*args: object, **kwargs: object) -> None:
            nonlocal crashed
            original_rebind(*args, **kwargs)  # type: ignore[arg-type]
            if not crashed:
                crashed = True
                raise RuntimeError("simulated crash after session rebind")

        monkeypatch.setattr(
            session_runtime_module, "rebind_session_task", crash_after_rebind
        )
    else:
        original_task_cas = tasks.compare_and_set_status
        crash_task_id = (
            "AID-1" if crash_boundary == "after_completed_task" else "AID-2"
        )
        crash_status = "done" if crash_task_id == "AID-1" else "in_progress"

        def crash_after_task_cas(
            task_id: str,
            *,
            expected_version: int,
            expected_status: str,
            status: str,
        ) -> Task:
            nonlocal crashed
            updated = original_task_cas(
                task_id,
                expected_version=expected_version,
                expected_status=expected_status,
                status=status,
            )
            if task_id == crash_task_id and status == crash_status and not crashed:
                crashed = True
                raise RuntimeError(f"simulated crash after {crash_task_id} CAS")
            return updated

        monkeypatch.setattr(tasks, "compare_and_set_status", crash_after_task_cas)

    with pytest.raises(RuntimeError, match="simulated crash"):
        guard.advance_next(
            "REQ-001", completed_phase=0, session_id="session-1", activated_by="codex"
        )
    assert crashed
    activation = gates.read_activation("REQ-001", 1)

    with pytest.raises(PhaseGateError, match="仍活跃"):
        guard.advance_next(
            "REQ-001", completed_phase=0, session_id="session-2", activated_by="codex"
        )

    end_session(
        workspace,
        "REQ-001",
        "session-1",
        task_provider=tasks,  # type: ignore[arg-type]
    )
    recovered = guard.advance_next(
        "REQ-001", completed_phase=0, session_id="session-2", activated_by="codex"
    )

    assert recovered.status == "in_progress"
    assert workspace.load("REQ-001")["meta"]["requirement_task_id"] == "AID-2"
    sessions = {item["id"]: item for item in workspace.load("REQ-001")["sessions"]}
    assert sessions["session-1"]["result"] == "detached"
    assert sessions["session-2"]["result"] == "in_progress"
    assert sessions["session-2"]["task_ids"] == ["AID-2"]
    assert gates.read_activation("REQ-001", 1) == activation
    assert tasks.tasks["AID-1"].status == "done"
    assert tasks.tasks["AID-2"].status == "in_progress"
    assert tasks.updates.count(("AID-1", "done")) == 1
    assert tasks.updates.count(("AID-2", "in_progress")) == 1


def test_transition_without_activation_still_requires_a_prebound_active_session(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    gates._write_gate_record(_record())
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )

    with pytest.raises(PhaseGateError, match="未绑定 Phase Task"):
        PhaseTransitionGuard(gates, tasks).advance_next(  # type: ignore[arg-type]
            "REQ-001", completed_phase=0, session_id="session-2", activated_by="codex"
        )

    assert workspace.load("REQ-001")["sessions"] == []
    assert not gates.activation_path_for("REQ-001", 1).exists()
    assert tasks.updates == []


def test_transition_resume_rejects_a_changed_committed_next_task(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workspace_orchestrator.automation.session_runtime import (
        attach_session,
        end_session,
    )

    workspace, git, gates = gate_store
    gates._write_gate_record(_record())
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )
    attach_session(
        workspace,
        "REQ-001",
        session_id="session-1",
        agent_name="codex",
        task_provider=tasks,  # type: ignore[arg-type]
        task_ids=("AID-1",),
    )
    original_meta_cas = WorkspaceStore.compare_and_set_meta
    crashed = False

    def crash_after_activation(
        store: WorkspaceStore, *args: object, **kwargs: object
    ) -> object:
        nonlocal crashed
        if store is workspace and not crashed:
            crashed = True
            raise RuntimeError("simulated crash after activation")
        return original_meta_cas(store, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        WorkspaceStore, "compare_and_set_meta", crash_after_activation
    )
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="simulated crash"):
        guard.advance_next(
            "REQ-001", completed_phase=0, session_id="session-1", activated_by="codex"
        )
    end_session(
        workspace,
        "REQ-001",
        "session-1",
        task_provider=tasks,  # type: ignore[arg-type]
    )
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, sha=SHA_1, next_task_id="AID-X")
    _install_definition(
        git, phase=1, sha=SHA_1, task_id="AID-X", next_task_id=None
    )

    with pytest.raises(PhaseGateError, match="已签发 Phase 0 GateDefinition 被当前提交改写"):
        guard.advance_next(
            "REQ-001", completed_phase=0, session_id="session-2", activated_by="codex"
        )

    assert workspace.load("REQ-001")["meta"]["requirement_task_id"] == "AID-1"
    assert all(
        session["id"] != "session-2"
        for session in workspace.load("REQ-001")["sessions"]
    )
    assert tasks.updates == []


def test_transition_task_cas_never_overwrites_concurrent_user_state(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    gates._write_gate_record(_record())
    workspace.write_json(
        workspace.path_for("REQ-001") / "sessions.json",
        [{"id": "session-1", "task_ids": ["AID-1"], "result": "in_progress"}],
    )

    class ConcurrentTasks(FakeTasks):
        def compare_and_set_status(
            self,
            task_id: str,
            *,
            expected_version: int,
            expected_status: str,
            status: str,
        ) -> Task:
            if task_id == "AID-1":
                self.tasks[task_id] = replace(
                    self.tasks[task_id], status="blocked", version=expected_version + 1
                )
                raise TaskProviderError("version conflict")
            return super().compare_and_set_status(
                task_id,
                expected_version=expected_version,
                expected_status=expected_status,
                status=status,
            )

    tasks = ConcurrentTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )

    with pytest.raises(PhaseGateError, match="version conflict"):
        PhaseTransitionGuard(gates, tasks).advance_next(  # type: ignore[arg-type]
            "REQ-001", completed_phase=0, session_id="session-1", activated_by="codex"
        )

    assert tasks.tasks["AID-1"].status == "blocked"
    assert tasks.tasks["AID-1"].version == 4
    assert gates.read_activation("REQ-001", 1).task_id == "AID-2"


def test_activation_remains_valid_on_dirty_descendant_commit(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, git, gates = gate_store
    _install_definition(
        gates.git,  # type: ignore[arg-type]
        phase=1,
        sha=SHA_0,
        task_id="AID-2",
        next_task_id=None,
    )
    gates._write_gate_record(_record())
    activation = PhaseActivationRecord(
        requirement_id="REQ-001",
        phase=1,
        task_id="AID-2",
        predecessor_gate_phase=0,
        predecessor_gate_commit_sha=SHA_0,
        predecessor_gate_record_fingerprint=gate_record_fingerprint(_record()),
        session_id="session-1",
        activated_at="2026-09-05T12:31:00+08:00",
        activated_by="codex",
    )
    workspace.touch_meta("REQ-001", requirement_task_id="AID-2")
    gates._write_activation(activation)
    git.head = SHA_1
    git.clean = False
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, sha=SHA_1)
    _install_definition(
        git, phase=1, sha=SHA_1, task_id="AID-2", next_task_id=None
    )

    assert gates.require_task_active("REQ-001", "AID-2") == "activated"


def test_activation_with_future_timestamp_is_rejected(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    gates._write_gate_record(_record())
    activation = PhaseActivationRecord(
        requirement_id="REQ-001",
        phase=1,
        task_id="AID-2",
        predecessor_gate_phase=0,
        predecessor_gate_commit_sha=SHA_0,
        predecessor_gate_record_fingerprint=gate_record_fingerprint(_record()),
        session_id="session-1",
        activated_at="2999-09-05T12:31:00+08:00",
        activated_by="codex",
    )

    with pytest.raises(PhaseGateError, match="不能是未来时间"):
        gates._write_activation(activation)


def test_phase_cli_exposes_receipt_issue_and_advance_commands() -> None:
    parser = build_parser()

    receipt = parser.parse_args(
        [
            "phase",
            "run-verification",
            "REQ-001",
            "--phase",
            "0",
            "--suite",
            "quality",
        ]
    )
    issue = parser.parse_args(
        ["phase", "issue", "REQ-001", "--phase", "0", "--file", "gate.json"]
    )
    attest = parser.parse_args(
        [
            "phase",
            "attest-review",
            "REQ-001",
            "--phase",
            "0",
            "--file",
            "review.json",
        ]
    )
    advance = parser.parse_args(
        ["phase", "advance", "REQ-001", "--completed-phase", "0"]
    )
    reopen = parser.parse_args(
        ["phase", "reopen", "REQ-001", "--phase", "0", "--reason", "修复后重审"]
    )

    assert receipt.phase_command == "run-verification"
    assert receipt.suite == "quality"
    assert attest.phase_command == "attest-review"
    assert issue.phase == 0
    assert advance.completed_phase == 0
    assert reopen.phase == 0
    assert reopen.reason == "修复后重审"


def test_transition_guard_rejects_without_mutating_manual_in_progress_without_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="in_progress", version=7),
        },
        [],
        [],
        [],
    )
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]

    with pytest.raises(PhaseGateError, match="缺少前序 Phase Gate"):
        guard.advance_next(
            "REQ-001",
            completed_phase=0,
            session_id="session-1",
            activated_by="codex",
        )

    assert tasks.tasks["AID-2"].status == "in_progress"
    assert tasks.updates == []
    assert tasks.tasks["AID-2"].version == 7


def test_gated_requirement_rejects_every_undeclared_task(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    _install_definition(
        git, phase=1, sha=SHA_0, task_id="AID-2", next_task_id=None
    )

    with pytest.raises(PhaseGateError, match="未在 GateDefinition 链中声明"):
        gates.require_task_active("REQ-001", "AID-EVIL")


def test_malformed_definition_chain_cannot_activate_undeclared_next_task(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, git, gates = gate_store
    _install_definition(git, next_task_id="AID-X")
    _install_definition(
        git, phase=1, sha=SHA_0, task_id="AID-2", next_task_id=None
    )
    workspace.write_json(
        workspace.path_for("REQ-001") / "sessions.json",
        [{"id": "session-1", "task_ids": ["AID-1"], "result": "in_progress"}],
    )
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-X": Task("AID-X", "Undeclared", status="blocked", version=4),
        },
        [],
        [],
        [],
    )

    with pytest.raises(PhaseGateError, match="next_task_id 未精确指向"):
        gates._write_gate_record(_record())

    assert tasks.tasks["AID-X"].status == "blocked"
    assert not gates.activation_path_for("REQ-001", 1).exists()


def test_malformed_phase_gate_flag_cannot_disable_enforcement(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    workspace.touch_meta("REQ-001", phase_gate_required="false")

    with pytest.raises(PhaseGateError, match="必须是布尔值"):
        gates.require_task_active("REQ-001", "AID-EVIL")


def test_requirement_completion_requires_closed_definition_chain_and_final_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    gates._write_gate_record(_record())

    _install_definition(
        git, phase=1, sha=SHA_0, task_id="AID-2", next_task_id="AID-3"
    )
    with pytest.raises(PhaseGateError, match="阶段链不闭合"):
        gates.require_requirement_completion_ready("REQ-001")

    assert git.files is not None
    git.files.pop((SHA_0, GateStore.definition_path("REQ-001", 1)))
    git.files.pop((SHA_0, "plans/phase-1.md"))
    _install_definition(git, next_task_id=None)
    gates.require_requirement_completion_ready("REQ-001")


def test_phase_one_accepts_direct_predecessor_whose_sha_is_ancestor(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_phase_receipts(
        gates,
        phase=1,
        sha=SHA_1,
        started_at="2026-09-05T12:32:00+08:00",
        completed_at="2026-09-05T12:40:00+08:00",
    )
    plan_one = "# Phase 1\n\nRuntime.\n"
    acceptance_one = ({"id": "P1-AC-01", "description": "runtime contract"},)
    _install_definition(
        git,
        phase=1,
        sha=SHA_1,
        plan_source=plan_one,
        acceptance=acceptance_one,
    )
    phase_one = _record(
        phase=1,
        sha=SHA_1,
        plan_source=plan_one,
        acceptance=acceptance_one,
        results=(
            AcceptanceResult(
                "P1-AC-01", "PASS", "contract pass", ("receipt-phase-1",)
            ),
        ),
        previous=phase_zero,
    )
    _install_activation(gates, phase_zero)

    gates._write_gate_record(phase_one)

    assert gates.read("REQ-001", 1) == phase_one


@pytest.mark.parametrize(
    "changed_field",
    ["acceptance", "verification_suites", "plan_source_path", "plan_source_fingerprint", "extension"],
)
@pytest.mark.parametrize("entrypoint", ["gate", "review", "task"])
def test_signed_predecessor_definition_cannot_drift_in_descendant_commit(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    changed_field: str,
    entrypoint: str,
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    _install_activation(gates, phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, phase=1, sha=SHA_1)
    _install_phase_receipts(
        gates, phase=1, sha=SHA_1,
        started_at="2026-09-05T12:32:00+08:00",
        completed_at="2026-09-05T12:40:00+08:00",
    )
    assert git.files is not None
    definition_key = (SHA_1, GateStore.definition_path("REQ-001", 0))
    payload = json.loads(git.files[definition_key])
    if changed_field == "acceptance":
        payload[changed_field][0]["description"] = "relaxed after approval"
    elif changed_field == "verification_suites":
        payload[changed_field][0]["commands"] = [["python", "-c", "pass"]]
    elif changed_field == "plan_source_path":
        payload[changed_field] = "plans/alternate.md"
        git.files[(SHA_1, "plans/alternate.md")] = PLAN_0_SOURCE
    elif changed_field == "plan_source_fingerprint":
        source = "# Changed signed plan\n"
        payload[changed_field] = source_fingerprint(source)
        git.files[(SHA_1, payload["plan_source_path"])] = source
    else:
        payload[changed_field] = {"future_policy": "relaxed"}
    git.files[definition_key] = json.dumps(payload)

    with pytest.raises(PhaseGateError, match="已签发 Phase 0 GateDefinition 被当前提交改写"):
        if entrypoint == "gate":
            gates._write_gate_record(_record(phase=1, sha=SHA_1, previous=phase_zero))
        elif entrypoint == "review":
            gates.record_review_from_payload(
                "REQ-001", 1,
                {
                    "verification_receipt_refs": ["receipt-phase-1"],
                    "implementation_session_ids": ["implementation-session"],
                    "implementation_run_ids": ["run-phase-1"],
                    "verdict": "PASS",
                    "resolved_findings": [],
                },
                reviewer_session_id="review-session",
            )
        else:
            gates.require_task_active("REQ-001", "AID-2")
    assert not gates.path_for("REQ-001", 1).exists()
    assert not gates.review_path_for("REQ-001", 1).exists()


def test_signed_predecessor_definition_allows_json_formatting_only(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    _install_activation(gates, phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, phase=1, sha=SHA_1)
    assert git.files is not None
    key = (SHA_1, GateStore.definition_path("REQ-001", 0))
    git.files[key] = json.dumps(json.loads(git.files[key]), sort_keys=True, indent=4)

    assert gates.require_task_active("REQ-001", "AID-2") == "activated"


def test_signed_predecessor_plan_source_cannot_drift_independently(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    _install_activation(gates, phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, phase=1, sha=SHA_1)
    assert git.files is not None
    git.files[(SHA_1, "plans/phase-0.md")] = "# Changed signed plan\n"

    with pytest.raises(PhaseGateError, match="计划源 digest"):
        gates.require_task_active("REQ-001", "AID-2")


def test_phase_one_gate_requires_its_activation_record(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_phase_receipts(
        gates,
        phase=1,
        sha=SHA_1,
        started_at="2026-09-05T12:32:00+08:00",
        completed_at="2026-09-05T12:40:00+08:00",
    )
    _install_definition(git, phase=1, sha=SHA_1)

    with pytest.raises(PhaseGateError, match="缺少 Phase 1 Activation Record"):
        gates._write_gate_record(_record(phase=1, sha=SHA_1, previous=phase_zero))


def test_phase_one_gate_rejects_receipt_started_before_activation(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_phase_receipts(gates, phase=1, sha=SHA_1)
    _install_definition(git, phase=1, sha=SHA_1)
    _install_activation(gates, phase_zero)
    early_receipt = _receipt(
        "receipt-phase-1", sha=SHA_1, run_id="run-phase-1"
    )
    phase_one = replace(
        _record(phase=1, sha=SHA_1, previous=phase_zero),
        verification_receipt_fingerprints=(
            verification_receipt_fingerprint(early_receipt),
        ),
    )

    with pytest.raises(PhaseGateError, match="早于本阶段 Activation"):
        gates._write_gate_record(phase_one)


def test_phase_one_rejects_missing_predecessor(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    git.head = SHA_1
    _install_phase_receipts(gates, phase=1, sha=SHA_1)
    _install_definition(git, phase=1, sha=SHA_1)
    phase_one = replace(
        _record(phase=1, sha=SHA_1),
        previous_gate_phase=0,
        previous_gate_commit_sha=SHA_0,
        previous_gate_record_fingerprint="f" * 64,
    )

    with pytest.raises(PhaseGateError, match="缺少前序"):
        gates._write_gate_record(phase_one)


def test_phase_one_rejects_predecessor_that_is_not_an_ancestor(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    git.head = SHA_1
    _install_phase_receipts(gates, phase=1, sha=SHA_1)
    phase_one = _record(phase=1, sha=SHA_1, previous=phase_zero)
    _install_definition(git, phase=1, sha=SHA_1)

    with pytest.raises(PhaseGateError, match="不包含前序"):
        gates._write_gate_record(phase_one)


@pytest.mark.parametrize(
    "field_change",
    [
        {"previous_gate_phase": 9},
        {"previous_gate_commit_sha": "2" * 40},
        {"previous_gate_record_fingerprint": "f" * 64},
    ],
)
def test_phase_one_rejects_tampered_predecessor_reference(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    field_change: dict[str, object],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates._write_gate_record(phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_phase_receipts(gates, phase=1, sha=SHA_1)
    _install_definition(git, phase=1, sha=SHA_1)
    phase_one = replace(_record(phase=1, sha=SHA_1, previous=phase_zero), **field_change)

    with pytest.raises(PhaseGateError, match="previous_gate"):
        gates._write_gate_record(phase_one)


def test_phase_one_rejects_persisted_predecessor_that_no_longer_passes(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, git, gates = gate_store
    phase_zero = _record()
    path = gates._write_gate_record(phase_zero)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "FAIL"
    workspace.write_json(path, payload)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_phase_receipts(gates, phase=1, sha=SHA_1)
    _install_definition(git, phase=1, sha=SHA_1)

    with pytest.raises(PhaseGateError, match="Phase Gate 未通过"):
        gates._write_gate_record(_record(phase=1, sha=SHA_1, previous=phase_zero))


@pytest.mark.parametrize(
    ("tampered_field", "tampered_value", "message"),
    [
        ("requirement_id", "REQ-999", "另一个 Requirement"),
        ("phase", 7, "phase 与路径不一致"),
    ],
)
def test_phase_one_rejects_predecessor_in_wrong_requirement_or_phase_slot(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    tampered_field: str,
    tampered_value: object,
    message: str,
) -> None:
    workspace, git, gates = gate_store
    phase_zero = _record()
    path = gates._write_gate_record(phase_zero)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[tampered_field] = tampered_value
    workspace.write_json(path, payload)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_phase_receipts(gates, phase=1, sha=SHA_1)
    _install_definition(git, phase=1, sha=SHA_1)

    with pytest.raises(PhaseGateError, match=message):
        gates._write_gate_record(_record(phase=1, sha=SHA_1, previous=phase_zero))


def test_validate_is_read_only_and_rejects_unknown_requirement(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceStore(tmp_path)
    gates = GateStore(workspace, FakeGit())

    with pytest.raises(PhaseGateError, match="未找到工作区"):
        gates.validate(_record())
    assert not workspace.path_for("REQ-001").exists()


def test_local_git_revision_reader_uses_exact_head_and_ancestry(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=tmp_path, check=True)
    git = LocalGitProvider(tmp_path)
    first = git.head_sha()
    assert git.read_file_at(first, "tracked.txt") == "first\n"
    tracked.write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "second"], cwd=tmp_path, check=True)
    second = git.head_sha()

    assert len(first) == len(second) == 40
    assert git.is_ancestor(first, second)
    assert not git.is_ancestor(second, first)
    (tmp_path / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    assert not git.is_clean()


def _production_review_packet(*, renewed: bool = False) -> dict[str, object]:
    suffix = "-new" if renewed else ""
    return {
        "verification_receipt_refs": [RECEIPT_CI + suffix, RECEIPT_GATE + suffix],
        "implementation_session_ids": ["implementation-session"],
        "implementation_run_ids": ["run-ci" + suffix, "run-gate" + suffix],
        "verdict": "PASS",
        "resolved_findings": ["all blockers resolved"],
    }


def _production_issue_packet(*, renewed: bool = False) -> dict[str, object]:
    suffix = "-new" if renewed else ""
    return {
        "acceptance_results": [
            AcceptanceResult(
                "P0-AC-06", "PASS", "CI pass", (RECEIPT_CI + suffix,)
            ).to_dict(),
            AcceptanceResult(
                "P0-AC-07", "PASS", "Gate pass", (RECEIPT_GATE + suffix,)
            ).to_dict(),
        ],
        "regression_summary": "all regression tests passed",
    }


def _install_production_candidate(
    gates: GateStore, monkeypatch: pytest.MonkeyPatch
) -> PhaseGateRecord:
    from workspace_orchestrator.phase_verification import PhaseVerificationRunner

    monkeypatch.setattr(PhaseVerificationRunner, "revalidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "workspace_orchestrator.phase_gate.now_iso", lambda: "2026-09-05T12:00:00+08:00"
    )
    gates.record_review_from_payload(
        "REQ-001", 0, _production_review_packet(), reviewer_session_id="reviewer"
    )
    monkeypatch.setattr(
        "workspace_orchestrator.phase_gate.now_iso", lambda: "2026-09-05T12:01:00+08:00"
    )
    return gates.issue_from_payload(
        "REQ-001", 0, _production_issue_packet(), issued_by="codex:issuer"
    )


def test_production_review_and_issue_retries_keep_original_time_and_live_checks(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workspace_orchestrator.phase_verification import PhaseVerificationRunner

    _, _, gates = gate_store
    original_gate = _install_production_candidate(gates, monkeypatch)
    original_review = gates.read_review("REQ-001", 0)
    checked: list[str] = []
    monkeypatch.setattr(
        PhaseVerificationRunner,
        "revalidate",
        lambda self, requirement_id, *, phase, receipt: checked.append(receipt.receipt_id),
    )
    monkeypatch.setattr(
        "workspace_orchestrator.phase_gate.now_iso", lambda: "2026-09-05T12:02:00+08:00"
    )

    assert gates.record_review_from_payload(
        "REQ-001", 0, _production_review_packet(), reviewer_session_id="reviewer"
    ) == original_review
    assert gates.issue_from_payload(
        "REQ-001", 0, _production_issue_packet(), issued_by="codex:issuer"
    ) == original_gate
    assert checked == [RECEIPT_CI, RECEIPT_GATE]

    def failed_revalidation(*args: object, **kwargs: object) -> None:
        raise PhaseGateError("验证失效")

    monkeypatch.setattr(PhaseVerificationRunner, "revalidate", failed_revalidation)
    with pytest.raises(PhaseGateError, match="验证失效"):
        gates.issue_from_payload(
            "REQ-001", 0, _production_issue_packet(), issued_by="codex:issuer"
        )
    assert gates.read("REQ-001", 0) == original_gate


def test_production_retry_cannot_silently_replace_changed_review_or_issue(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, gates = gate_store
    original = _install_production_candidate(gates, monkeypatch)
    with pytest.raises(PhaseGateError, match="不可覆盖"):
        gates.record_review_from_payload(
            "REQ-001",
            0,
            {**_production_review_packet(), "resolved_findings": ["changed findings"]},
            reviewer_session_id="reviewer",
        )
    with pytest.raises(PhaseGateError, match="不可覆盖"):
        gates.issue_from_payload(
            "REQ-001",
            0,
            {**_production_issue_packet(), "regression_summary": "changed summary"},
            issued_by="codex:issuer",
        )
    assert gates.read("REQ-001", 0) == original


@pytest.mark.parametrize("renewed_sha", [SHA_0, SHA_1])
def test_reopen_preserves_history_and_allows_new_sha_or_ci_receipts(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
    renewed_sha: str,
) -> None:
    workspace, git, gates = gate_store
    _install_production_candidate(gates, monkeypatch)
    old_review = workspace.read_json(gates.review_path_for("REQ-001", 0))
    old_gate = workspace.read_json(gates.path_for("REQ-001", 0))
    old_gate["future_metadata"] = {"keep": ["all", "fields"]}
    workspace.write_json(gates.path_for("REQ-001", 0), old_gate)
    git.head = renewed_sha
    _install_definition(git, sha=renewed_sha)
    _install_definition(git, phase=1, sha=renewed_sha, next_task_id=None)
    journal = gates.reopen("REQ-001", 0, reason="修复后重审", session_id="operator")
    assert journal["status"] == "completed"
    history = list((workspace.path_for("REQ-001") / "phase-history" / "phase-0").glob("*.json"))
    assert len(history) == 1
    archived = workspace.read_json(history[0])
    assert archived["review"] == old_review
    assert archived["gate"] == old_gate
    assert archived["reason"] == "修复后重审"
    assert archived["reopened_by"] == "operator"
    assert not gates.path_for("REQ-001", 0).exists()
    assert not gates.review_path_for("REQ-001", 0).exists()
    assert gates.read_verification_receipt("REQ-001", RECEIPT_CI).commit_sha == SHA_0
    assert gates.reopen(
        "REQ-001", 0, reason="修复后重审", session_id="resumed-operator"
    ) == journal

    for receipt_id, run_id in [(RECEIPT_CI, "run-ci"), (RECEIPT_GATE, "run-gate")]:
        gates._write_verification_receipt(
            _receipt(receipt_id + "-new", sha=renewed_sha, run_id=run_id + "-new")
        )
    gates.record_review_from_payload(
        "REQ-001", 0, _production_review_packet(renewed=True), reviewer_session_id="reviewer"
    )
    new_gate = gates.issue_from_payload(
        "REQ-001", 0, _production_issue_packet(renewed=True), issued_by="codex:issuer"
    )
    assert new_gate.commit_sha == renewed_sha
    assert new_gate.verification_receipt_refs == (RECEIPT_CI + "-new", RECEIPT_GATE + "-new")
    assert workspace.read_json(history[0]) == archived
    second = gates.reopen("REQ-001", 0, reason="修复后重审", session_id="operator")
    assert second["archive_fingerprint"] != journal["archive_fingerprint"]
    assert len(list(history[0].parent.glob("*.json"))) == 2
    assert workspace.read_json(history[0]) == archived


def test_reopen_review_only_candidate_and_missing_candidate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    with pytest.raises(PhaseGateError, match="没有可重审"):
        gates.reopen("REQ-001", 0, reason="重新验证", session_id="operator")
    assert not gates.reopen_path_for("REQ-001", 0).exists()
    review = gates.record_review_from_payload(
        "REQ-001", 0, _production_review_packet(), reviewer_session_id="reviewer"
    )
    journal = gates.reopen("REQ-001", 0, reason="重新验证", session_id="operator")
    archive = journal["archive"]
    assert isinstance(archive, dict)
    assert archive["review"] == review.to_dict()
    assert archive["gate"] is None
    assert not gates.review_path_for("REQ-001", 0).exists()
    assert workspace.load("REQ-001")["meta"]["requirement_task_id"] == "AID-1"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("next_activation", "后序 Phase 已激活"),
        ("wrong_task", "当前未完成"),
        ("done", "当前未完成"),
        ("ungated", "启用阶段门禁"),
        ("wrong_phase", "未在完整"),
        ("empty_reason", "原因"),
        ("empty_session", "Session"),
    ],
)
def test_reopen_rejects_out_of_scope_without_mutation(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    case: str,
    message: str,
) -> None:
    workspace, _, gates = gate_store
    record = _record()
    gates._write_gate_record(record)
    if case == "next_activation":
        # 即使 Activation 写入后、meta CAS 前崩溃，也不能重开被它引用的 Gate。
        workspace.write_json(gates.activation_path_for("REQ-001", 1), {"durable": True})
    elif case == "wrong_task":
        workspace.touch_meta("REQ-001", requirement_task_id="AID-2")
    elif case == "done":
        workspace.touch_meta("REQ-001", status="done")
    elif case == "ungated":
        workspace.touch_meta("REQ-001", phase_gate_required=False)
    with pytest.raises(PhaseGateError, match=message):
        gates.reopen(
            "REQ-001",
            99 if case == "wrong_phase" else 0,
            reason=" " if case == "empty_reason" else "修复后重审",
            session_id=" " if case == "empty_session" else "operator",
        )
    assert gates.read("REQ-001", 0) == record
    assert not gates.reopen_path_for("REQ-001", 0).exists()
    assert not (workspace.path_for("REQ-001") / "phase-history").exists()


@pytest.mark.parametrize("boundary", ["journal", "archive", "review", "gate", "completed"])
def test_reopen_recovers_every_durable_boundary_without_losing_facts(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    workspace, _, gates = gate_store
    original_gate = _install_production_candidate(gates, monkeypatch)
    original_review = gates.read_review("REQ-001", 0)
    write_json = workspace.write_json
    unlink = Path.unlink
    injected = False

    def crash_after_write(path: Path, value: object) -> None:
        nonlocal injected
        write_json(path, value)
        is_journal = path == gates.reopen_path_for("REQ-001", 0)
        if not injected and (
            (boundary == "archive" and path.parent.name == "phase-0")
            or (boundary == "journal" and is_journal)
            or (
                boundary == "completed"
                and is_journal
                and isinstance(value, dict)
                and value.get("status") == "completed"
            )
        ):
            injected = True
            raise RuntimeError("injected reopen crash")

    def crash_after_unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal injected
        unlink(path, missing_ok=missing_ok)
        target = (
            gates.review_path_for("REQ-001", 0)
            if boundary == "review"
            else gates.path_for("REQ-001", 0)
        )
        if not injected and boundary in {"review", "gate"} and path == target:
            injected = True
            raise RuntimeError("injected reopen crash")

    monkeypatch.setattr(WorkspaceStore, "write_json", staticmethod(crash_after_write))
    monkeypatch.setattr(Path, "unlink", crash_after_unlink)
    with pytest.raises(RuntimeError, match="injected reopen crash"):
        gates.reopen("REQ-001", 0, reason="恢复候选", session_id="original-operator")
    pending = workspace.read_json(gates.reopen_path_for("REQ-001", 0))
    if boundary != "completed":
        with pytest.raises(PhaseGateError, match="重审事务未完成"):
            gates.validate(original_gate)
        with pytest.raises(PhaseGateError, match="重审事务未完成"):
            gates.record_review_from_payload(
                "REQ-001", 0, _production_review_packet(), reviewer_session_id="reviewer"
            )
        with pytest.raises(PhaseGateError, match="重审事务未完成"):
            gates.issue_from_payload(
                "REQ-001", 0, _production_issue_packet(), issued_by="codex:issuer"
            )
    journal = gates.reopen("REQ-001", 0, reason="恢复候选", session_id="new-operator")
    assert journal == {**pending, "status": "completed"}
    history = list((workspace.path_for("REQ-001") / "phase-history" / "phase-0").glob("*.json"))
    assert len(history) == 1
    archived = workspace.read_json(history[0])
    assert archived["gate"] == original_gate.to_dict()
    assert archived["review"] == original_review.to_dict()
    assert archived["reopened_by"] == "original-operator"
    assert not gates.path_for("REQ-001", 0).exists()
    assert not gates.review_path_for("REQ-001", 0).exists()
    assert gates.reopen("REQ-001", 0, reason="恢复候选", session_id="retry") == journal


def test_reopen_pending_recovery_refuses_changed_candidate_or_new_activation(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _, gates = gate_store
    record = _install_production_candidate(gates, monkeypatch)
    write_json = workspace.write_json

    def crash_after_journal(path: Path, value: object) -> None:
        write_json(path, value)
        if path == gates.reopen_path_for("REQ-001", 0):
            raise RuntimeError("journal persisted")

    monkeypatch.setattr(WorkspaceStore, "write_json", staticmethod(crash_after_journal))
    with pytest.raises(RuntimeError, match="journal persisted"):
        gates.reopen("REQ-001", 0, reason="恢复候选", session_id="operator")
    monkeypatch.setattr(WorkspaceStore, "write_json", staticmethod(write_json))
    with pytest.raises(PhaseGateError, match="原原因"):
        gates.reopen("REQ-001", 0, reason="另一个原因", session_id="operator")
    current_review = workspace.read_json(gates.review_path_for("REQ-001", 0))
    current_review["new_candidate_metadata"] = True
    write_json(gates.review_path_for("REQ-001", 0), current_review)
    with pytest.raises(PhaseGateError, match="当前候选已变化"):
        gates.reopen("REQ-001", 0, reason="恢复候选", session_id="operator")
    assert workspace.read_json(gates.review_path_for("REQ-001", 0)) == current_review
    assert gates.read("REQ-001", 0) == record
    write_json(gates.activation_path_for("REQ-001", 1), {"durable": True})
    with pytest.raises(PhaseGateError, match="后序 Phase 已激活"):
        gates.reopen("REQ-001", 0, reason="恢复候选", session_id="operator")
    assert gates.read("REQ-001", 0) == record


def test_reopen_cli_requires_reason_and_derives_session(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workspace_orchestrator import cli

    workspace, _, gates = gate_store
    gates._write_gate_record(_record())
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["phase", "reopen", "REQ-001", "--phase", "0"])
    monkeypatch.setattr(cli, "WorkspaceStore", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(cli, "GateStore", lambda store: gates)
    monkeypatch.setattr(cli, "require_session_id", lambda provider: "runtime-session")
    args = parser.parse_args(
        [
            "--root", str(workspace.project_root),
            "phase", "reopen", "REQ-001", "--phase", "0", "--reason", "重新审查",
        ]
    )
    result = json.loads(cli.run(args))
    assert result["archive"]["reopened_by"] == "runtime-session"
    assert result["archive"]["reason"] == "重新审查"
    assert result["status"] == "completed"


def test_advance_rechecks_pending_reopen_after_validation_before_activation(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _, gates = gate_store
    gates._write_gate_record(_record())
    sessions = [{"id": "session-1", "task_ids": ["AID-1"], "result": "in_progress"}]
    sessions_path = workspace.path_for("REQ-001") / "sessions.json"
    workspace.write_json(sessions_path, sessions)
    tasks = FakeTasks(
        {
            "AID-1": Task("AID-1", "Phase 0", status="in_review", version=3),
            "AID-2": Task("AID-2", "Phase 1", status="blocked", version=4),
        },
        [],
        [],
        [],
    )
    write_activation = gates._write_activation
    write_json = workspace.write_json

    def crash_after_reopen_journal(path: Path, value: object) -> None:
        write_json(path, value)
        if path == gates.reopen_path_for("REQ-001", 0):
            raise RuntimeError("reopen crashed after journal")

    def interleave_reopen(activation: PhaseActivationRecord) -> Path:
        # advance 的第一次 Gate 校验已完成；另一操作在获取 Activation 锁前
        # 写入 reopen journal 后崩溃，Activation 必须在锁内重新核对前序链。
        with monkeypatch.context() as crash:
            crash.setattr(
                WorkspaceStore, "write_json", staticmethod(crash_after_reopen_journal)
            )
            with pytest.raises(RuntimeError, match="reopen crashed"):
                gates.reopen("REQ-001", 0, reason="必须重审", session_id="operator")
        return write_activation(activation)

    monkeypatch.setattr(gates, "_write_activation", interleave_reopen)
    with pytest.raises(PhaseGateError, match="重审事务未完成"):
        PhaseTransitionGuard(gates, tasks).advance_next(  # type: ignore[arg-type]
            "REQ-001", completed_phase=0, session_id="session-1", activated_by="codex"
        )

    assert not gates.activation_path_for("REQ-001", 1).exists()
    assert workspace.load("REQ-001")["meta"]["requirement_task_id"] == "AID-1"
    assert workspace.read_json(sessions_path) == sessions
    assert tasks.tasks["AID-1"].status == "in_review"
    assert tasks.tasks["AID-2"].status == "blocked"
    assert tasks.updates == []
    assert gates.reopen(
        "REQ-001", 0, reason="必须重审", session_id="operator"
    )["status"] == "completed"
