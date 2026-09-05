from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path

import pytest

from workspace_orchestrator import workspace as workspace_module
from workspace_orchestrator.adapters.git import LocalGitProvider
from workspace_orchestrator.models import Task
from workspace_orchestrator.phase_gate import (
    AcceptanceResult,
    GateStore,
    PhaseGateError,
    PhaseGateRecord,
    PhaseTransitionGuard,
    ReviewAttestation,
    content_fingerprint,
    gate_record_fingerprint,
    source_fingerprint,
)
from workspace_orchestrator.workspace import WORKSPACE_FILES, WorkspaceStore

SHA_0 = "0" * 40
SHA_1 = "1" * 40
PLAN_0_SOURCE = "# Phase 0\n\nAudit and exact-SHA gate.\n"
ACCEPTANCE_0 = (
    {"id": "P0-AC-06", "description": "CI and type checking pass"},
    {"id": "P0-AC-07", "description": "exact-SHA phase gate"},
)


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


def _install_definition(
    git: FakeGit,
    *,
    phase: int = 0,
    sha: str = SHA_0,
    task_id: str | None = None,
    plan_source: str = PLAN_0_SOURCE,
    acceptance: tuple[dict[str, str], ...] = ACCEPTANCE_0,
    next_task_id: str | None = "AID-2",
) -> None:
    files = git.files if git.files is not None else {}
    git.files = files
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
            "next_task_id": next_task_id,
        }
    )


@dataclass
class FakeTasks:
    task: Task
    updates: list[str]

    def get_task(self, task_id: str) -> Task:
        assert task_id == self.task.id
        return self.task

    def update_status(self, task_id: str, status: str) -> Task:
        assert task_id == self.task.id
        self.updates.append(status)
        self.task = replace(self.task, status=status, version=(self.task.version or 0) + 1)
        return self.task


def _review(
    *,
    reviewer: str = "review-session",
    implementers: tuple[str, ...] = ("implementation-session",),
    verdict: str = "PASS",
) -> ReviewAttestation:
    return ReviewAttestation(
        reviewer_session_id=reviewer,
        implementation_session_ids=implementers,
        implementation_run_ids=("run-001",),
        verdict=verdict,
        resolved_findings=("finding-001",),
        reviewed_at="2026-09-05T12:00:00+08:00",
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
            AcceptanceResult("P0-AC-06", "PASS", "CI pass", ("receipt:ci",)),
            AcceptanceResult("P0-AC-07", "PASS", "Gate pass", ("receipt:unit",)),
        ),
        verification_receipt_refs=("verification:common-quality",),
        regression_summary="All V1 and V2 tests passed.",
        review_attestation=review or _review(),
        issued_at="2026-09-05T12:30:00+08:00",
        issued_by="phase-gate-test",
        status=status,
    )


@pytest.fixture
def gate_store(tmp_path: Path) -> tuple[WorkspaceStore, FakeGit, GateStore]:
    workspace = WorkspaceStore(tmp_path)
    assert workspace.create("Phase gate", task_provider=None) == "REQ-001"
    git = FakeGit()
    _install_definition(git)
    return workspace, git, GateStore(workspace, git)


def test_phase_zero_gate_is_written_atomically_to_optional_workspace_directory(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, _, gates = gate_store
    record = _record()

    path = gates.write(record)

    assert path == workspace.path_for("REQ-001") / "phase-gates" / "phase-0.json"
    assert gates.read("REQ-001", 0) == record
    assert "phase-gates" not in WORKSPACE_FILES
    assert workspace.load("REQ-001")["meta"]["id"] == "REQ-001"


def test_issue_derives_head_fingerprints_and_predecessor_reference(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = gates.issue(
        requirement_id="req-001",
        phase=0,
        acceptance_results=(
            AcceptanceResult("P0-AC-06", "PASS", "pass", ("receipt:ci",)),
            AcceptanceResult("P0-AC-07", "PASS", "pass", ("receipt:gate",)),
        ),
        verification_receipt_refs=("verification:quality",),
        regression_summary="regression pass",
        review_attestation=_review(),
        issued_at="2026-09-05T12:00:00+08:00",
        issued_by="gate-issuer",
    )
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    acceptance_one = ({"id": "P1-AC-01", "description": "runtime contract"},)
    _install_definition(
        git,
        phase=1,
        sha=SHA_1,
        plan_source="# Phase 1\n",
        acceptance=acceptance_one,
    )
    phase_one = gates.issue(
        requirement_id="REQ-001",
        phase=1,
        acceptance_results=(
            AcceptanceResult("P1-AC-01", "PASS", "pass", ("receipt:runtime",)),
        ),
        verification_receipt_refs=("verification:quality",),
        regression_summary="regression pass",
        review_attestation=_review(),
        issued_at="2026-09-05T13:00:00+08:00",
        issued_by="gate-issuer",
    )

    assert phase_zero.commit_sha == SHA_0
    assert phase_zero.plan_fingerprint == source_fingerprint(PLAN_0_SOURCE)
    assert phase_one.previous_gate_phase == 0
    assert phase_one.previous_gate_commit_sha == SHA_0
    assert phase_one.previous_gate_record_fingerprint == gate_record_fingerprint(phase_zero)


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
        gates.write(_record(sha=sha))


def test_gate_rejects_record_for_stale_head(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    git.head = SHA_1

    with pytest.raises(PhaseGateError, match="SHA 已陈旧"):
        gates.write(_record())


def test_gate_rejects_dirty_worktree(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    git.clean = False

    with pytest.raises(PhaseGateError, match="未提交变更"):
        gates.write(_record())


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
        gates.write(_record())


def test_duplicate_acceptance_ids_are_rejected(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    results = (
        AcceptanceResult("P0-AC-07", "PASS", "first"),
        AcceptanceResult("P0-AC-07", "PASS", "duplicate"),
    )

    with pytest.raises(PhaseGateError, match="Acceptance ID 重复"):
        gates.write(_record(results=results))


def test_any_failed_acceptance_rejects_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    results = (AcceptanceResult("P0-AC-07", "FAIL", "not ready"),)

    with pytest.raises(PhaseGateError, match="未全部 PASS"):
        gates.write(_record(results=results))


@pytest.mark.parametrize(
    "results",
    [
        (AcceptanceResult("P0-AC-07", "PASS", "pass", ("receipt:gate",)),),
        (
            AcceptanceResult("P0-AC-06", "PASS", "pass", ("receipt:ci",)),
            AcceptanceResult("P0-AC-07", "PASS", "pass", ("receipt:gate",)),
            AcceptanceResult("P0-AC-99", "PASS", "extra", ("receipt:extra",)),
        ),
    ],
)
def test_acceptance_results_must_exactly_cover_definition_ids(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
    results: tuple[AcceptanceResult, ...],
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match="精确覆盖"):
        gates.write(_record(results=results))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"verification_receipt_refs": ()}, "Verification Receipt"),
        (
            {
                "results": (
                    AcceptanceResult("P0-AC-06", "PASS", "pass", ()),
                    AcceptanceResult("P0-AC-07", "PASS", "pass", ("receipt:gate",)),
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
        gates.write(record)


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
        gates.write(_record(review=review))


def test_gate_status_must_be_pass(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store

    with pytest.raises(PhaseGateError, match="Phase Gate 未通过"):
        gates.write(_record(status="FAIL"))


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
        gates.write(_record())

    assert not path.exists()
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))
    assert workspace.load("REQ-001")["meta"]["id"] == "REQ-001"


def test_same_gate_write_is_idempotent_but_different_record_cannot_overwrite(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    record = _record()
    path = gates.write(record)
    before = path.read_bytes()

    assert gates.write(record) == path
    with pytest.raises(PhaseGateError, match="不可覆盖"):
        gates.write(replace(record, issued_at="2026-09-05T13:00:00+08:00"))
    assert path.read_bytes() == before


def test_transition_guard_advances_only_after_valid_exact_sha_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    gates.write(_record())
    tasks = FakeTasks(Task("AID-2", "Phase 1", status="blocked", version=4), [])
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]

    advanced = guard.advance_next("REQ-001", completed_phase=0, next_task_id="AID-2")

    assert advanced.status == "in_progress"
    assert advanced.version == 5
    assert tasks.updates == ["in_progress"]


def test_transition_guard_rejects_and_converges_manual_in_progress_without_gate(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, _, gates = gate_store
    tasks = FakeTasks(Task("AID-2", "Phase 1", status="in_progress", version=7), [])
    guard = PhaseTransitionGuard(gates, tasks)  # type: ignore[arg-type]

    with pytest.raises(PhaseGateError, match="缺少前序 Phase Gate"):
        guard.advance_next("REQ-001", completed_phase=0, next_task_id="AID-2")

    assert tasks.task.status == "blocked"
    assert tasks.task.version == 8
    assert tasks.updates == ["blocked"]


def test_phase_one_accepts_direct_predecessor_whose_sha_is_ancestor(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates.write(phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
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
            AcceptanceResult("P1-AC-01", "PASS", "contract pass", ("receipt:contract",)),
        ),
        previous=phase_zero,
    )

    gates.write(phase_one)

    assert gates.read("REQ-001", 1) == phase_one


def test_phase_one_rejects_missing_predecessor(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    git.head = SHA_1
    _install_definition(git, phase=1, sha=SHA_1)
    phase_one = replace(
        _record(phase=1, sha=SHA_1),
        previous_gate_phase=0,
        previous_gate_commit_sha=SHA_0,
        previous_gate_record_fingerprint="f" * 64,
    )

    with pytest.raises(PhaseGateError, match="缺少前序"):
        gates.write(phase_one)


def test_phase_one_rejects_predecessor_that_is_not_an_ancestor(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    _, git, gates = gate_store
    phase_zero = _record()
    gates.write(phase_zero)
    git.head = SHA_1
    phase_one = _record(phase=1, sha=SHA_1, previous=phase_zero)
    _install_definition(git, phase=1, sha=SHA_1)

    with pytest.raises(PhaseGateError, match="不包含前序"):
        gates.write(phase_one)


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
    gates.write(phase_zero)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, phase=1, sha=SHA_1)
    phase_one = replace(_record(phase=1, sha=SHA_1, previous=phase_zero), **field_change)

    with pytest.raises(PhaseGateError, match="previous_gate"):
        gates.write(phase_one)


def test_phase_one_rejects_persisted_predecessor_that_no_longer_passes(
    gate_store: tuple[WorkspaceStore, FakeGit, GateStore],
) -> None:
    workspace, git, gates = gate_store
    phase_zero = _record()
    path = gates.write(phase_zero)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "FAIL"
    workspace.write_json(path, payload)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, phase=1, sha=SHA_1)

    with pytest.raises(PhaseGateError, match="Phase Gate 未通过"):
        gates.write(_record(phase=1, sha=SHA_1, previous=phase_zero))


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
    path = gates.write(phase_zero)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[tampered_field] = tampered_value
    workspace.write_json(path, payload)
    git.head = SHA_1
    git.ancestors = {(SHA_0, SHA_1)}
    _install_definition(git, phase=1, sha=SHA_1)

    with pytest.raises(PhaseGateError, match=message):
        gates.write(_record(phase=1, sha=SHA_1, previous=phase_zero))


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
