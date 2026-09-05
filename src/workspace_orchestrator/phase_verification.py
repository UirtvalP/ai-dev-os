"""Controlled Phase verification runners for the bootstrap gate protocol."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .phase_gate import (
    GateStore,
    PhaseGateError,
    VerificationReceipt,
    VerificationSuiteDefinition,
)
from .workspace import now_iso

JsonReader = Callable[[str], Mapping[str, object]]
COMMAND_TIMEOUT_SECONDS = 900


def _read_github_json(url: str) -> Mapping[str, object]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-dev-os-phase-verifier",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise PhaseGateError(f"无法读取 GitHub Actions 事实：{exc}") from exc
    if not isinstance(payload, Mapping):
        raise PhaseGateError("GitHub API 返回的不是 JSON 对象")
    return payload


def _objects(payload: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    values = payload.get(key)
    if not isinstance(values, list):
        raise PhaseGateError(f"GitHub API 缺少 {key} 数组")
    result: list[Mapping[str, object]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise PhaseGateError(f"GitHub API {key} 包含无效对象")
        result.append(value)
    return tuple(result)


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PhaseGateError(f"GitHub run 缺少 {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PhaseGateError(f"GitHub run {label} 不是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise PhaseGateError(f"GitHub run {label} 必须包含时区")
    return parsed.isoformat()


class PhaseVerificationRunner:
    """Executes only suites committed in the exact-HEAD GateDefinition."""

    def __init__(self, gates: GateStore, *, json_reader: JsonReader = _read_github_json) -> None:
        self.gates = gates
        self.json_reader = json_reader

    def run(
        self,
        requirement_id: str,
        *,
        phase: int,
        suite_id: str,
        session_id: str,
    ) -> VerificationReceipt:
        if not session_id.strip():
            raise PhaseGateError("Verification Runner 必须绑定当前 Session")
        commit_sha = self.gates.git.head_sha()
        if not self.gates.git.is_clean():
            raise PhaseGateError("验证开始前工作树必须干净")
        suite = self.gates.verification_suite(
            requirement_id, phase, suite_id, revision=commit_sha
        )
        if suite.kind == "command":
            receipt = self._run_commands(
                requirement_id.upper(), commit_sha, suite, session_id
            )
        elif suite.kind == "github-actions":
            receipt = self._import_github_actions(
                requirement_id.upper(), commit_sha, suite, session_id
            )
        else:  # pragma: no cover - GateDefinition rejects this first.
            raise PhaseGateError(f"不支持 Verification Suite kind={suite.kind}")
        if self.gates.git.head_sha() != commit_sha or not self.gates.git.is_clean():
            raise PhaseGateError("验证期间 HEAD 或工作树发生变化，Receipt 已拒绝")
        self.gates._write_verification_receipt(receipt)
        return receipt

    def revalidate(
        self,
        requirement_id: str,
        *,
        phase: int,
        receipt: VerificationReceipt,
    ) -> None:
        """Live-check a stored receipt without minting replacement evidence."""

        normalized = requirement_id.upper()
        commit_sha = self.gates.git.head_sha()
        if not self.gates.git.is_clean():
            raise PhaseGateError("Gate 签发重验前工作树必须干净")
        if receipt.requirement_id != normalized or receipt.commit_sha != commit_sha:
            raise PhaseGateError("Verification Receipt 未绑定当前 Requirement exact SHA")
        suite = self.gates.verification_suite(
            normalized, phase, receipt.suite_id, revision=commit_sha
        )
        self._require_static_binding(receipt, suite)
        if suite.kind == "command":
            if receipt.environment != self._local_environment() or receipt.source_url is not None:
                raise PhaseGateError("本地 Verification Receipt 环境或来源不匹配")
            self._execute_commands(suite)
        elif suite.kind == "github-actions":
            match = re.fullmatch(
                r"github-actions-([1-9][0-9]*)-attempt-([1-9][0-9]*)",
                receipt.run_id,
            )
            if match is None:
                raise PhaseGateError("GitHub Verification Receipt 缺少可信 run ID/attempt")
            run_id = match.group(1)
            run_attempt = match.group(2)
            base = f"https://api.github.com/repos/{suite.repository}"
            run = self.json_reader(f"{base}/actions/runs/{run_id}")
            facts = self._github_run_facts(
                suite,
                commit_sha,
                run,
                expected_run_id=run_id,
                expected_attempt=run_attempt,
            )
            expected = self._github_receipt_fields(suite, facts)
            actual = (
                receipt.run_id,
                receipt.environment,
                receipt.started_at,
                receipt.completed_at,
                receipt.source_url,
                receipt.summary,
            )
            if actual != expected:
                raise PhaseGateError("GitHub Verification Receipt 与实时 API 事实不一致")
        else:  # pragma: no cover - GateDefinition rejects this first.
            raise PhaseGateError(f"不支持 Verification Suite kind={suite.kind}")
        if self.gates.git.head_sha() != commit_sha or not self.gates.git.is_clean():
            raise PhaseGateError("Gate 签发重验期间 HEAD 或工作树发生变化")

    @staticmethod
    def _local_environment() -> str:
        return f"{platform.platform()} / Python {platform.python_version()}"

    @staticmethod
    def _require_static_binding(
        receipt: VerificationReceipt, suite: VerificationSuiteDefinition
    ) -> None:
        if receipt.suite_fingerprint != suite.fingerprint:
            raise PhaseGateError("Verification Receipt Suite 指纹不匹配")
        if receipt.issuer != suite.expected_issuer:
            raise PhaseGateError("Verification Receipt 不是由声明执行器签发")
        if receipt.command != suite.command_summary:
            raise PhaseGateError("Verification Receipt 命令与提交内 Suite 不一致")
        if receipt.status != "PASS" or receipt.exit_code != 0:
            raise PhaseGateError("Verification Receipt 不是成功结果")

    def _execute_commands(self, suite: VerificationSuiteDefinition) -> list[str]:
        summaries: list[str] = []
        for command in suite.commands:
            try:
                result = subprocess.run(
                    command,
                    cwd=self.gates.workspace_store.working_root,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise PhaseGateError(
                    f"Verification 命令超过 {COMMAND_TIMEOUT_SECONDS}s：{command[0]}"
                ) from exc
            except OSError as exc:
                raise PhaseGateError(f"无法执行 Verification 命令 {command[0]}：{exc}") from exc
            output = (result.stdout + "\n" + result.stderr).strip()
            summaries.append(f"{' '.join(command)} => {result.returncode}: {output[-1000:]}")
            if result.returncode != 0:
                raise PhaseGateError(
                    f"Verification Suite {suite.suite_id} 失败：{summaries[-1]}"
                )
        return summaries

    def _run_commands(
        self,
        requirement_id: str,
        commit_sha: str,
        suite: VerificationSuiteDefinition,
        session_id: str,
    ) -> VerificationReceipt:
        started_at = now_iso()
        summaries = self._execute_commands(suite)
        run_id = str(uuid.uuid4())
        return VerificationReceipt(
            receipt_id=f"{suite.suite_id}-{run_id}",
            requirement_id=requirement_id,
            commit_sha=commit_sha,
            suite_id=suite.suite_id,
            suite_fingerprint=suite.fingerprint,
            issuer=suite.expected_issuer,
            run_id=run_id,
            session_id=session_id,
            command=suite.command_summary,
            environment=self._local_environment(),
            started_at=started_at,
            completed_at=now_iso(),
            exit_code=0,
            status="PASS",
            summary="\n".join(summaries),
        )

    def _import_github_actions(
        self,
        requirement_id: str,
        commit_sha: str,
        suite: VerificationSuiteDefinition,
        session_id: str,
    ) -> VerificationReceipt:
        workflow = quote(str(suite.workflow), safe="")
        query = urlencode(
            {
                "head_sha": commit_sha,
                "status": "completed",
                "event": suite.required_event,
                "per_page": 20,
            }
        )
        base = f"https://api.github.com/repos/{suite.repository}"
        runs = _objects(
            self.json_reader(f"{base}/actions/workflows/{workflow}/runs?{query}"),
            "workflow_runs",
        )
        successful = [
            run
            for run in runs
            if run.get("head_sha") == commit_sha
            and run.get("conclusion") == "success"
            and run.get("event") == suite.required_event
        ]
        if not successful:
            raise PhaseGateError(
                "没有找到绑定当前 exact SHA 与 required_event 的成功 GitHub Actions run"
            )
        run = max(successful, key=lambda item: int(str(item.get("run_number") or 0)))
        facts = self._github_run_facts(suite, commit_sha, run)
        (
            receipt_run_id,
            environment,
            started_at,
            completed_at,
            source_url,
            summary,
        ) = self._github_receipt_fields(suite, facts)
        return VerificationReceipt(
            receipt_id=(
                f"{suite.suite_id}-{facts['run_id']}-attempt-{facts['run_attempt']}"
            ),
            requirement_id=requirement_id,
            commit_sha=commit_sha,
            suite_id=suite.suite_id,
            suite_fingerprint=suite.fingerprint,
            issuer=suite.expected_issuer,
            run_id=receipt_run_id,
            session_id=session_id,
            command=suite.command_summary,
            environment=environment,
            started_at=started_at,
            completed_at=completed_at,
            exit_code=0,
            status="PASS",
            summary=summary,
            source_url=source_url,
        )

    def _github_run_facts(
        self,
        suite: VerificationSuiteDefinition,
        commit_sha: str,
        run: Mapping[str, object],
        *,
        expected_run_id: str | None = None,
        expected_attempt: str | None = None,
    ) -> dict[str, str]:
        run_id = str(run.get("id") or "").strip()
        if re.fullmatch(r"[1-9][0-9]*", run_id) is None:
            raise PhaseGateError("GitHub Actions run 缺少可信 numeric ID")
        if expected_run_id is not None and run_id != expected_run_id:
            raise PhaseGateError("GitHub Actions API run ID 与请求不一致")
        raw_attempt = run.get("run_attempt")
        if isinstance(raw_attempt, bool) or not isinstance(raw_attempt, int) or raw_attempt < 1:
            raise PhaseGateError("GitHub Actions run 缺少可信 run_attempt")
        run_attempt = str(raw_attempt)
        if expected_attempt is not None and run_attempt != expected_attempt:
            raise PhaseGateError("GitHub Actions API run_attempt 与 Receipt 不一致")
        if (
            run.get("head_sha") != commit_sha
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("event") != suite.required_event
        ):
            raise PhaseGateError("GitHub Actions run 未绑定 exact SHA/event 成功完成")
        repository = run.get("repository")
        if not isinstance(repository, Mapping) or repository.get("full_name") != suite.repository:
            raise PhaseGateError("GitHub Actions run repository 与声明仓库不匹配")
        workflow = str(suite.workflow)
        expected_path = (
            workflow if workflow.startswith(".github/workflows/") else f".github/workflows/{workflow}"
        )
        if run.get("path") != expected_path:
            raise PhaseGateError("GitHub Actions run workflow path 与声明不匹配")
        base = f"https://api.github.com/repos/{suite.repository}"
        jobs_url = run.get("jobs_url")
        expected_jobs_url = f"{base}/actions/runs/{run_id}/jobs"
        if jobs_url != expected_jobs_url:
            raise PhaseGateError("GitHub Actions run 的 jobs_url 与目标仓库不匹配")
        attempt_jobs_url = (
            f"{base}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        )
        jobs = self._read_all_jobs(attempt_jobs_url)
        invalid: list[str] = []
        for name in suite.required_jobs:
            matching = [job for job in jobs if job.get("name") == name]
            if len(matching) != 1:
                invalid.append(f"{name}({len(matching)} matches)")
                continue
            job = matching[0]
            job_id = str(job.get("id") or "")
            if (
                re.fullmatch(r"[1-9][0-9]*", job_id) is None
                or str(job.get("run_id") or "") != run_id
                or job.get("head_sha") != commit_sha
                or job.get("status") != "completed"
                or job.get("conclusion") != "success"
            ):
                invalid.append(name)
        if invalid:
            raise PhaseGateError(
                "GitHub Actions 必需 Job 不唯一、身份不符或未通过："
                + ", ".join(invalid)
            )
        source_url = str(run.get("html_url") or "").strip()
        expected_source_url = f"https://github.com/{suite.repository}/actions/runs/{run_id}"
        if source_url != expected_source_url:
            raise PhaseGateError("GitHub Actions run URL 与目标仓库或 run ID 不匹配")
        return {
            "run_id": run_id,
            "run_attempt": run_attempt,
            "started_at": _timestamp(
                run.get("run_started_at") or run.get("created_at"), "started_at"
            ),
            "completed_at": _timestamp(run.get("updated_at"), "completed_at"),
            "source_url": source_url,
        }

    @staticmethod
    def _github_receipt_fields(
        suite: VerificationSuiteDefinition, facts: Mapping[str, str]
    ) -> tuple[str, str, str, str, str, str]:
        run_id = facts["run_id"]
        return (
            f"github-actions-{run_id}-attempt-{facts['run_attempt']}",
            "GitHub Actions: " + ", ".join(suite.required_jobs),
            facts["started_at"],
            facts["completed_at"],
            facts["source_url"],
            (
                f"GitHub Actions run {run_id} attempt {facts['run_attempt']}; "
                "required jobs all succeeded"
            ),
        )

    def _read_all_jobs(self, attempt_jobs_url: str) -> tuple[Mapping[str, object], ...]:
        jobs: list[Mapping[str, object]] = []
        page = 1
        total_count: int | None = None
        while total_count is None or len(jobs) < total_count:
            payload = self.json_reader(
                f"{attempt_jobs_url}?per_page=100&page={page}"
            )
            raw_total = payload.get("total_count")
            if isinstance(raw_total, bool) or not isinstance(raw_total, int) or raw_total < 0:
                raise PhaseGateError("GitHub Jobs API 缺少可信 total_count")
            if total_count is None:
                total_count = raw_total
            elif total_count != raw_total:
                raise PhaseGateError("GitHub Jobs API 分页期间 total_count 发生变化")
            current = _objects(payload, "jobs")
            if not current and len(jobs) < total_count:
                raise PhaseGateError("GitHub Jobs API 分页不完整")
            jobs.extend(current)
            if len(jobs) > total_count:
                raise PhaseGateError("GitHub Jobs API 返回数量超过 total_count")
            page += 1
        ids = [str(job.get("id") or "") for job in jobs]
        if len(ids) != len(set(ids)):
            raise PhaseGateError("GitHub Jobs API 返回重复 Job ID")
        return tuple(jobs)
