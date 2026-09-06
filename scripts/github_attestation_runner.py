"""受保护 GitHub workflow 使用的 Phase 4 Receipt 生成器。

该文件必须从 ``github.workflow_sha`` 对应的 trusted checkout 执行；不得从候选
checkout 导入或执行 attestor 代码。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SHA1 = re.compile(r"[0-9a-f]{40}")
_OUTPUT_LIMIT = 4000
_FORBIDDEN_ENV = frozenset(("PYTHONPATH", "PYTHONHOME"))


def canonical(value: object) -> bytes:
    """本 policy 只允许 string/int/bool/null，等价于相应 RFC 8785 表示。"""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load_policy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("attestor policy 必须是 JSON object")
    claimed = payload.pop("policy_fingerprint", None)
    if claimed != fingerprint(payload):
        raise ValueError("attestor policy fingerprint 不匹配")
    payload["policy_fingerprint"] = claimed
    if payload.get("schema_version") != 1:
        raise ValueError("attestor policy schema 不受支持")
    return payload


def github_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-dev-os-phase4-attestor",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise TypeError("GitHub API 返回无效 JSON")
    return payload


def candidate_tree(repository: str, candidate_sha: str, token: str) -> str:
    commit = github_json(
        f"https://api.github.com/repos/{repository}/git/commits/{candidate_sha}", token,
    )
    tree = commit.get("tree")
    if commit.get("sha") != candidate_sha or not isinstance(tree, dict):
        raise ValueError("candidate SHA/tree 不存在或不匹配")
    value = tree.get("sha")
    if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
        raise ValueError("candidate tree 无效")
    return value


def _artifact(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def _suite_contract(suite_id: str, item: dict[str, Any]) -> list[dict[str, object]]:
    kind = item["kind"]
    if kind == "command":
        commands = item["commands"]
        return [
            {
                "suite_id": f"{suite_id}::{index}",
                "suite_type": command["suite_type"],
                "argv": command["argv"],
                "timeout_seconds": command.get("timeout_seconds", 900),
                "cwd": ".",
                "environment_allowlist": [],
                "environment": {},
                "artifacts": command.get("artifacts", []),
                "requires_network": False,
                "network_reader_id": None,
            }
            for index, command in enumerate(commands, 1)
        ]
    if kind == "github-actions":
        return [{
            "suite_id": suite_id,
            "suite_type": "integration",
            "argv": [
                "github-actions", item["repository"], item["workflow"],
                item["required_event"], *item["required_jobs"],
            ],
            "timeout_seconds": 900,
            "cwd": ".",
            "environment_allowlist": [],
            "environment": {},
            "artifacts": [],
            "requires_network": True,
            "network_reader_id": "github-actions-api",
        }]
    raise ValueError(f"不支持 suite kind={kind}")


def _github_ci_evidence(
    suite: dict[str, Any], run_id: str, attempt: int, candidate_sha: str,
    token: str, output: Path,
) -> tuple[str, int, str, str, list[dict[str, object]]]:
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None or attempt < 1:
        raise ValueError("GitHub CI run id/attempt 无效")
    repository = suite["repository"]
    base = f"https://api.github.com/repos/{repository}"
    run = github_json(f"{base}/actions/runs/{run_id}/attempts/{attempt}", token)
    repo = run.get("repository")
    expected_path = f".github/workflows/{suite['workflow']}"
    if (
        str(run.get("id")) != run_id
        or run.get("run_attempt") != attempt
        or run.get("head_sha") != candidate_sha
        or run.get("path") != expected_path
        or run.get("event") != suite["required_event"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not isinstance(repo, dict)
        or repo.get("full_name") != repository
    ):
        raise ValueError("GitHub CI run 未绑定目标 repository/workflow/event/exact SHA")
    jobs: list[object] = []
    page = 1
    total_count: int | None = None
    while page <= 100:
        page_payload = github_json(
            f"{base}/actions/runs/{run_id}/attempts/{attempt}/jobs"
            f"?per_page=100&page={page}",
            token,
        )
        page_jobs = page_payload.get("jobs")
        raw_total = page_payload.get("total_count")
        if (
            not isinstance(page_jobs, list)
            or isinstance(raw_total, bool)
            or not isinstance(raw_total, int)
            or raw_total < 0
        ):
            raise ValueError("GitHub CI jobs 无效")
        if total_count is None:
            total_count = raw_total
        elif raw_total != total_count:
            raise ValueError("GitHub CI jobs 分页期间发生变化")
        jobs.extend(page_jobs)
        if len(jobs) >= total_count:
            break
        if len(page_jobs) != 100:
            raise ValueError("GitHub CI jobs 分页不完整")
        page += 1
    if total_count is None or len(jobs) != total_count:
        raise ValueError("GitHub CI jobs 分页不完整")
    for name in suite["required_jobs"]:
        matching = [job for job in jobs if isinstance(job, dict) and job.get("name") == name]
        if (
            len(matching) != 1
            or matching[0].get("status") != "completed"
            or matching[0].get("conclusion") != "success"
        ):
            raise ValueError(f"GitHub CI required job 未唯一通过：{name}")
    evidence = {
        "repository": repository,
        "workflow": expected_path,
        "event": suite["required_event"],
        "candidate_sha": candidate_sha,
        "run_id": run_id,
        "run_attempt": attempt,
        "required_jobs": suite["required_jobs"],
        "source_url": run.get("html_url"),
    }
    evidence_path = output / "github-ci-evidence.json"
    evidence_path.write_bytes(canonical(evidence))
    started = str(run.get("run_started_at") or run.get("created_at"))
    completed = str(run.get("updated_at"))
    return (
        f"github-actions-{run_id}-attempt-{attempt}", attempt, started, completed,
        [_artifact(evidence_path, output)],
    )


def _bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")


def _preview(value: bytes) -> str:
    return value[:_OUTPUT_LIMIT].decode("utf-8", errors="replace")


def _command_artifacts(
    constraints: object, candidate_root: Path,
) -> tuple[list[dict[str, object]], str | None]:
    if not isinstance(constraints, list):
        return [], "artifact_contract_invalid"
    artifacts: list[dict[str, object]] = []
    root = candidate_root.resolve(strict=True)
    for constraint in constraints:
        if not isinstance(constraint, dict):
            return [], "artifact_contract_invalid"
        raw_path = constraint.get("path")
        required = constraint.get("required", True)
        max_bytes = constraint.get("max_bytes")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or Path(raw_path).is_absolute()
            or ".." in Path(raw_path).parts
            or not isinstance(required, bool)
            or max_bytes is not None
            and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0)
        ):
            return [], "artifact_contract_invalid"
        path = candidate_root / raw_path
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            if required:
                return [], "artifact_missing"
            continue
        if root not in resolved.parents or not resolved.is_file():
            return [], "artifact_outside_workspace"
        if max_bytes is not None and resolved.stat().st_size > max_bytes:
            return [], "artifact_too_large"
        artifacts.append(_artifact(resolved, root))
    return artifacts, None


def _execute_commands(
    contract: list[dict[str, object]], candidate_root: Path, *, candidate_user: str | None = None,
) -> tuple[list[dict[str, object]], str, str, list[dict[str, object]]]:
    root = candidate_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("candidate root 必须是已存在目录")
    started_at = datetime.now(UTC).isoformat()
    results: list[dict[str, object]] = []
    all_artifacts: list[dict[str, object]] = []
    environment = {
        key: value for key, value in os.environ.items() if key.upper() not in _FORBIDDEN_ENV
    }
    for suite in contract:
        argv = suite["argv"]
        timeout = suite["timeout_seconds"]
        cwd = root / str(suite["cwd"])
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(value, str) or "\0" in value for value in argv)
            or isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout < 1
            or root not in cwd.resolve(strict=True).parents and cwd.resolve(strict=True) != root
        ):
            raise ValueError("command suite contract 无效")
        began = time.monotonic()
        status, returncode, error_code = "ERROR", None, None
        stdout = stderr = b""
        try:
            command = list(argv)
            if candidate_user is not None:
                command = [
                    "sudo", "--non-interactive", "--user", candidate_user, "--",
                    "env", "-i", f"PATH={environment.get('PATH', '')}",
                    f"HOME={root / '.phase4-home'}", *command,
                ]
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                capture_output=True,
                check=False,
                timeout=timeout,
                shell=False,
            )
            stdout, stderr = completed.stdout, completed.stderr
            returncode = completed.returncode
            status = "PASS" if returncode == 0 else "FAIL"
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _bytes(exc.stdout), _bytes(exc.stderr)
            error_code = "timeout"
        except OSError as exc:
            stderr = str(exc).encode("utf-8", errors="replace")
            error_code = "process_unavailable"
        finally:
            if candidate_user is not None:
                subprocess.run(
                    ["sudo", "--non-interactive", "pkill", "--signal", "KILL", "--uid", candidate_user],
                    capture_output=True,
                    check=False,
                    timeout=10,
                    shell=False,
                )
        artifacts, artifact_error = _command_artifacts(suite["artifacts"], root)
        if artifact_error is not None:
            status, error_code = "ERROR", artifact_error
        all_artifacts.extend(artifacts)
        results.append({
            "suite_id": suite["suite_id"],
            "suite_type": suite["suite_type"],
            "status": status,
            "returncode": returncode,
            "duration_seconds": math.ceil(time.monotonic() - began),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stdout_preview": _preview(stdout),
            "stderr_preview": _preview(stderr),
            "artifacts": artifacts,
            "error_code": error_code,
        })
    return results, started_at, datetime.now(UTC).isoformat(), all_artifacts


def build_receipt(
    policy: dict[str, Any], *, suite_id: str, candidate_sha: str, candidate_tree_sha: str,
    github_run_id: str, github_run_attempt: int, token: str, output: Path,
    candidate_root: Path | None = None, ci_run_id: str | None = None,
    ci_run_attempt: int | None = None, candidate_user: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    suites = policy.get("suites")
    if not isinstance(suites, dict) or suite_id not in suites:
        raise ValueError("suite 未获受保护 policy 授权")
    suite = suites[suite_id]
    if not isinstance(suite, dict):
        raise TypeError("suite policy 无效")
    contract = _suite_contract(suite_id, suite)
    environment = {
        "runner_environment": "github-hosted",
        "runner_os": platform.system(),
        "python": platform.python_version(),
    }
    plan: dict[str, object] = {
        "project_id": policy["project_id"],
        "requirement_id": policy["requirement_id"],
        "phase": policy["phase"],
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree_sha,
        "policy_fingerprint": policy["policy_fingerprint"],
        "environment_digest": fingerprint(environment),
        "suites": contract,
        "required_suite_ids": [item["suite_id"] for item in contract],
        "mode": "collect-all",
    }
    artifacts: list[dict[str, object]] = []
    if suite["kind"] == "command":
        if candidate_root is None:
            raise ValueError("command suite 必须指定 candidate root")
        results, started, completed, artifacts = _execute_commands(
            contract, candidate_root, candidate_user=candidate_user,
        )
        receipt_run_id = f"github-attestation-{github_run_id}-attempt-{github_run_attempt}"
        receipt_attempt = github_run_attempt
    else:
        if ci_run_id is None or ci_run_attempt is None:
            raise ValueError("GitHub suite 必须指定已完成 CI run id/attempt")
        receipt_run_id, receipt_attempt, started, completed, artifacts = _github_ci_evidence(
            suite, ci_run_id, ci_run_attempt, candidate_sha, token, output,
        )
        summary = canonical({
            "run_id": ci_run_id, "run_attempt": ci_run_attempt,
            "required_jobs": suite["required_jobs"],
        })
        results = [{
            "suite_id": contract[0]["suite_id"],
            "suite_type": contract[0]["suite_type"],
            "status": "PASS",
            "returncode": 0,
            "duration_seconds": 0,
            "stdout_sha256": hashlib.sha256(summary).hexdigest(),
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            "stdout_preview": _preview(summary),
            "stderr_preview": "",
            "artifacts": artifacts,
            "error_code": None,
        }]
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": f"{suite_id}-{receipt_run_id}",
        "project_id": policy["project_id"],
        "requirement_id": policy["requirement_id"],
        "phase": policy["phase"],
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree_sha,
        "plan_fingerprint": fingerprint(plan),
        "policy_fingerprint": policy["policy_fingerprint"],
        "run_id": receipt_run_id,
        "attempt": receipt_attempt,
        "environment_digest": plan["environment_digest"],
        "started_at": started,
        "completed_at": completed,
        "results": results,
        "result": "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL",
        "artifact_digest": fingerprint(artifacts),
        "provider_id": suite["provider_id"],
        "provider_version": "github-oidc-v1",
        "legacy_receipt_fingerprint": None,
    }
    return plan, receipt


def build_envelope(
    policy: dict[str, Any], receipt: dict[str, object], *, run_id: str, attempt: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "algorithm": "sigstore-github-oidc",
        "repository": policy["repository"],
        "workflow_path": policy["workflow_path"],
        "workflow_ref": policy["workflow_ref"],
        "attestor_run_id": run_id,
        "attestor_run_attempt": attempt,
        "subject_sha256": hashlib.sha256(canonical(receipt)).hexdigest(),
        "payload": receipt,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--suite-id", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--candidate-user")
    parser.add_argument("--ci-run-id")
    parser.add_argument("--ci-run-attempt", type=int)
    args = parser.parse_args()
    if _SHA1.fullmatch(args.candidate_sha) is None:
        parser.error("candidate SHA 必须是 40 位小写十六进制")
    token = os.environ.get("GITHUB_TOKEN", "")
    github_run_id = os.environ.get("GITHUB_RUN_ID", "")
    github_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    if not token or re.fullmatch(r"[1-9][0-9]*", github_run_id) is None or github_attempt < 1:
        parser.error("必须在 GitHub Actions run/attempt 上下文执行")
    policy = load_policy(args.policy)
    if policy.get("repository") != os.environ.get("GITHUB_REPOSITORY"):
        parser.error("GitHub repository 与受保护 policy 不匹配")
    args.output.mkdir(parents=True, exist_ok=True)
    tree = candidate_tree(policy["repository"], args.candidate_sha, token)
    plan, receipt = build_receipt(
        policy, suite_id=args.suite_id, candidate_sha=args.candidate_sha,
        candidate_tree_sha=tree, github_run_id=github_run_id,
        github_run_attempt=github_attempt, token=token, output=args.output,
        candidate_root=args.candidate_root, ci_run_id=args.ci_run_id,
        ci_run_attempt=args.ci_run_attempt, candidate_user=args.candidate_user,
    )
    envelope = build_envelope(
        policy, receipt, run_id=github_run_id, attempt=github_attempt,
    )
    (args.output / "phase4-plan.json").write_bytes(canonical(plan))
    (args.output / "phase4-receipt.json").write_bytes(canonical(receipt))
    (args.output / "phase4-envelope.json").write_bytes(canonical(envelope))
    return 0 if receipt["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
