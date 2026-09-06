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
import stat
import subprocess
import tempfile
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


def _run_checked(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        command, cwd=cwd, capture_output=True, check=False, timeout=30, shell=False,
    )
    if completed.returncode != 0:
        detail = _preview(completed.stderr or completed.stdout)
        raise RuntimeError(f"可信执行准备失败：{command[0]} ({completed.returncode}) {detail}")
    return completed


def _candidate_identity(candidate_user: str) -> tuple[str, str, str]:
    uid = _preview(_run_checked(["/usr/bin/id", "--user", candidate_user]).stdout).strip()
    gid = _preview(_run_checked(["/usr/bin/id", "--group", candidate_user]).stdout).strip()
    group = _preview(_run_checked(["/usr/bin/id", "--group", "--name", candidate_user]).stdout).strip()
    current_uid = _preview(_run_checked(["/usr/bin/id", "--user"]).stdout).strip()
    groups = _preview(_run_checked(["/usr/bin/id", "--groups", candidate_user]).stdout).split()
    if (
        not uid.isdecimal() or int(uid) == 0 or uid == current_uid or not gid.isdecimal()
        or not group or groups != [gid]
    ):
        raise ValueError("candidate user 必须是独立的非 root 身份")
    sudo = subprocess.run(
        ["/usr/bin/sudo", "--non-interactive", "--user", candidate_user, "--",
         "/usr/bin/sudo", "--non-interactive", "--list"],
        capture_output=True, check=False, timeout=10, shell=False,
    )
    if sudo.returncode == 0:
        raise ValueError("candidate user 不得拥有 sudo 权限")
    return uid, gid, group


def _candidate_path_entry_safe(candidate_user: str, entry: str) -> bool:
    writable = subprocess.run(
        ["/usr/bin/sudo", "--non-interactive", "--user", candidate_user, "--",
         "/usr/bin/test", "-w", entry],
        capture_output=True, check=False, timeout=10, shell=False,
    )
    if writable.returncode == 0:
        return False
    if writable.returncode != 1:
        raise ValueError(f"candidate PATH 目录无法验证：{entry}")
    writable_file = subprocess.run(
        ["/usr/bin/sudo", "--non-interactive", "--user", candidate_user, "--",
         "/usr/bin/find", "-L", entry, "-maxdepth", "1", "-type", "f",
         "-writable", "-print", "-quit"],
        capture_output=True, check=False, timeout=30, shell=False,
    )
    return writable_file.returncode == 0 and not writable_file.stdout.strip()


def _verify_candidate_path(candidate_user: str, entries: list[str]) -> None:
    for entry in entries:
        if not _candidate_path_entry_safe(candidate_user, entry):
            raise ValueError(f"candidate PATH 目录或文件可写：{entry}")


def _candidate_path(
    candidate_user: str, path_value: str, *, command_names: tuple[str, ...], trusted_root: Path,
) -> str:
    entries = path_value.split(os.pathsep)
    if not entries or any(not entry or not Path(entry).is_absolute() for entry in entries):
        raise ValueError("candidate PATH 必须只包含绝对目录")
    existing = [entry for entry in entries if Path(entry).is_dir()]
    if not existing:
        raise ValueError("candidate PATH 不包含现存目录")
    verified = [
        entry for entry in existing if _candidate_path_entry_safe(candidate_user, entry)
    ]
    trusted_bin: Path | None = None
    for command_name in dict.fromkeys(command_names):
        if Path(command_name).name != command_name:
            raise ValueError("candidate command 必须通过受验证 PATH 解析")
        source = next(
            (Path(entry) / command_name for entry in existing
             if (Path(entry) / command_name).is_file()
             and os.access(Path(entry) / command_name, os.X_OK)),
            None,
        )
        if source is None:
            raise ValueError(f"candidate command 不存在：{command_name}")
        # 在任何候选代码运行前，从已解析的 runner 入口建立一次性只读基线。
        # O_NOFOLLOW + fstat 防止把符号链接或非普通文件冻结成可信入口。
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and not info.st_mode & 0o111):
                raise ValueError(f"candidate command 不是可执行普通文件：{command_name}")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read()
        finally:
            os.close(descriptor)
        if trusted_bin is None:
            trusted_bin = trusted_root / "trusted-bin"
            trusted_bin.mkdir(mode=0o700, exist_ok=True)
            if not trusted_bin.is_dir():
                raise ValueError("trusted-bin 必须是目录")
        target = trusted_bin / command_name
        target.write_bytes(content)
        target.chmod(0o555)
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(content).digest():
            raise ValueError("candidate command 冻结校验失败")
    if trusted_bin is not None:
        for target in trusted_bin.iterdir():
            target.chmod(0o555)
        trusted_bin.chmod(0o555)
        _run_checked([
            "/usr/bin/sudo", "--non-interactive", "/bin/chown", "--recursive",
            "root:root", str(trusted_bin),
        ])
        verified.insert(0, str(trusted_bin))
    return os.pathsep.join(verified)


def _verify_canonical_checkout(root: Path, candidate_sha: str) -> None:
    head = _preview(_run_checked(["/usr/bin/git", "rev-parse", "HEAD"], cwd=root).stdout).strip()
    if head != candidate_sha:
        raise ValueError("candidate checkout 未绑定 exact SHA")
    _run_checked(["/usr/bin/git", "diff", "--quiet", "HEAD", "--"], cwd=root)
    _run_checked(["/usr/bin/git", "diff", "--cached", "--quiet", "HEAD", "--"], cwd=root)


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _materialize_tree(candidate_root: Path, candidate_sha: str, destination: Path) -> None:
    listing = _run_checked(
        ["/usr/bin/git", "ls-tree", "-r", "-z", "--full-tree", candidate_sha],
        cwd=candidate_root,
    ).stdout
    destination_root = destination.resolve(strict=True)
    for record in listing.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3:
            raise ValueError("candidate tree 记录无效")
        mode, kind, expected_sha = (field.decode("ascii") for field in fields)
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("candidate tree 包含 symlink、gitlink 或特殊文件")
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("candidate tree 包含越界路径")
        source = candidate_root / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError("candidate checkout 与 tree 类型不匹配")
        content = source.read_bytes()
        if _git_blob_sha(content) != expected_sha:
            raise ValueError("candidate checkout 文件与 tree blob 不匹配")
        target = destination / relative
        resolved_parent = target.parent.resolve(strict=False)
        if resolved_parent != destination_root and destination_root not in resolved_parent.parents:
            raise ValueError("candidate tree 包含越界路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if mode == "100755" else 0o644)


def _fresh_candidate_copy(
    root: Path, candidate_sha: str, candidate_user: str, candidate_group: str,
) -> tuple[Path, Path]:
    # GitHub Workspace 挂载不会向动态创建的隔离 UID 授予可靠访问权限。
    temporary = Path(tempfile.mkdtemp(prefix="phase4-command-"))
    temporary.chmod(0o711)
    work = temporary / "work"
    work.mkdir()
    _materialize_tree(root, candidate_sha, work)
    home = temporary / "home"
    home.mkdir()
    _run_checked([
        "/usr/bin/sudo", "--non-interactive", "/bin/chown", "--recursive",
        f"{candidate_user}:{candidate_group}", str(work), str(home),
    ])
    return temporary, work


def _create_candidate_user(prefix: str, index: int) -> tuple[str, tuple[str, str, str]]:
    if re.fullmatch(r"[a-z_][a-z0-9_-]{0,19}", prefix) is None:
        raise ValueError("candidate user prefix 无效")
    name = f"{prefix}{index}"
    uid = 50000 + index
    for key in (name, str(uid)):
        exists = subprocess.run(
            ["/usr/bin/getent", "passwd", key], capture_output=True, check=False,
            timeout=10, shell=False,
        )
        if exists.returncode == 0 or exists.returncode not in (0, 2):
            raise ValueError("candidate user 身份已存在或无法验证")
    _run_checked([
        "/usr/bin/sudo", "--non-interactive", "/usr/sbin/useradd", "--uid", str(uid),
        "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", name,
    ])
    return name, _candidate_identity(name)


def _delete_candidate_user(candidate_user: str, candidate_uid: str) -> str | None:
    try:
        deleted = subprocess.run(
            ["/usr/bin/sudo", "--non-interactive", "/usr/sbin/userdel", "--force",
             candidate_user],
            capture_output=True, check=False, timeout=30, shell=False,
        )
        if deleted.returncode != 0:
            return "candidate_identity_cleanup_failed"
        for key in (candidate_user, candidate_uid):
            remaining = subprocess.run(
                ["/usr/bin/getent", "passwd", key], capture_output=True, check=False,
                timeout=10, shell=False,
            )
            if remaining.returncode != 2:
                return "candidate_identity_cleanup_failed"
    except (OSError, subprocess.TimeoutExpired):
        return "candidate_identity_cleanup_failed"
    return None


def _terminate_candidate(candidate_uid: str) -> str | None:
    try:
        killed = subprocess.run(
            ["/usr/bin/sudo", "--non-interactive", "/usr/bin/pkill", "--signal", "KILL",
             "--uid", candidate_uid],
            capture_output=True, check=False, timeout=10, shell=False,
        )
        if killed.returncode not in (0, 1):
            return "candidate_cleanup_failed"
        survivors = subprocess.run(
            ["/usr/bin/pgrep", "--uid", candidate_uid],
            capture_output=True, check=False, timeout=10, shell=False,
        )
        if survivors.returncode != 1:
            return "candidate_process_survived" if survivors.returncode == 0 else "candidate_cleanup_failed"
    except (OSError, subprocess.TimeoutExpired):
        return "candidate_cleanup_failed"
    return None


def _remove_candidate_copy(temporary: Path) -> str | None:
    try:
        removed = subprocess.run(
            ["/usr/bin/sudo", "--non-interactive", "/bin/rm", "--recursive", "--force", "--",
             str(temporary)],
            capture_output=True, check=False, timeout=30, shell=False,
        )
        if removed.returncode != 0 or temporary.exists():
            return "candidate_cleanup_failed"
    except (OSError, subprocess.TimeoutExpired):
        return "candidate_cleanup_failed"
    return None


def _execute_commands(
    contract: list[dict[str, object]], candidate_root: Path, *, candidate_sha: str | None = None,
    candidate_user: str | None = None,
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
    if candidate_user is not None:
        if candidate_sha is None or _SHA1.fullmatch(candidate_sha) is None:
            raise ValueError("隔离执行必须绑定 candidate SHA")
        _verify_canonical_checkout(root, candidate_sha)
        isolated_sha = candidate_sha
    else:
        isolated_sha = None
    isolated_path: str | None = None
    path_preparation_error: str | None = None
    trusted_tools: Path | None = None
    if candidate_user is not None:
        probe_user: str | None = None
        probe_identity: tuple[str, str, str] | None = None
        try:
            # GitHub Workspace 可能在 sudo/useradd 探测后禁止创建或写入新增子目录；
            # 可信工具不依赖候选 checkout，使用 OS 临时区并随后 root-own + 只读冻结。
            trusted_tools = Path(tempfile.mkdtemp(prefix="phase4-tools-"))
            trusted_tools.chmod(0o711)
            # 在切换到隔离候选身份前创建唯一可写子目录；部分托管 Runner 会在
            # useradd/sudo 身份探测后拒绝于 workspace 内新建子目录。
            (trusted_tools / "trusted-bin").mkdir(mode=0o700)
            probe_user, probe_identity = _create_candidate_user(candidate_user, 0)
            commands = tuple(
                str(suite["argv"][0]) for suite in contract
                if suite["argv"] != ["git", "diff", "--check", "origin/main", "HEAD"]
            ) + ("git", "realpath", "dirname")
            isolated_path = _candidate_path(
                probe_user, environment.get("PATH", ""),
                command_names=commands, trusted_root=trusted_tools,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            path_preparation_error = str(exc)
        finally:
            if probe_user is not None and probe_identity is not None:
                cleanup_error = _terminate_candidate(probe_identity[0])
                identity_error = _delete_candidate_user(probe_user, probe_identity[0])
                path_preparation_error = path_preparation_error or cleanup_error or identity_error
    for command_index, suite in enumerate(contract, 1):
        argv = suite["argv"]
        timeout = suite["timeout_seconds"]
        suite_cwd = str(suite["cwd"])
        cwd = root / suite_cwd
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
        temporary: Path | None = None
        execution_root = root
        active_user: str | None = None
        candidate_identity: tuple[str, str, str] | None = None
        try:
            command = list(argv)
            if candidate_user is not None:
                assert isolated_sha is not None
                _verify_canonical_checkout(root, isolated_sha)
                if command == ["git", "diff", "--check", "origin/main", "HEAD"]:
                    cwd = root / suite_cwd
                    command = [
                        "/usr/bin/git", "-c", "diff.external=", "-c", "diff.trustExitCode=false",
                        "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--check",
                        "origin/main", "HEAD",
                    ]
                else:
                    if isolated_path is None:
                        raise ValueError(path_preparation_error or "candidate PATH 基线准备失败")
                    active_user, candidate_identity = _create_candidate_user(
                        candidate_user, command_index,
                    )
                    _verify_candidate_path(active_user, isolated_path.split(os.pathsep))
                    temporary, execution_root = _fresh_candidate_copy(
                        root, isolated_sha, active_user, candidate_identity[2],
                    )
                    cwd = execution_root / suite_cwd
                    command = [
                        "/usr/bin/sudo", "--non-interactive", "--user", active_user, "--",
                        "/usr/bin/env", "-i", f"PATH={isolated_path}",
                        f"HOME={temporary / 'home'}",
                        f"TMPDIR={temporary / 'home'}", *command,
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
        except (RuntimeError, ValueError) as exc:
            stderr = str(exc).encode("utf-8", errors="replace")
            error_code = "trusted_preparation_failed"
        finally:
            if active_user is not None and candidate_identity is not None:
                cleanup_error = _terminate_candidate(candidate_identity[0])
                if cleanup_error is not None:
                    status, error_code = "ERROR", cleanup_error
        artifacts, artifact_error = _command_artifacts(suite["artifacts"], execution_root)
        if artifact_error is not None:
            status, error_code = "ERROR", artifact_error
        if temporary is not None:
            removal_error = _remove_candidate_copy(temporary)
            if removal_error is not None:
                status, error_code = "ERROR", removal_error
        if active_user is not None and candidate_identity is not None:
            identity_error = _delete_candidate_user(active_user, candidate_identity[0])
            if identity_error is not None:
                status, error_code = "ERROR", identity_error
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
    if trusted_tools is not None:
        cleanup_error = _remove_candidate_copy(trusted_tools)
        if cleanup_error is not None:
            for result in results:
                result["status"], result["error_code"] = "ERROR", cleanup_error
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
    declared_phase = suite.get("phase")
    suite_phase = declared_phase if declared_phase is not None else policy.get("phase")
    if (
        type(suite_phase) is not int
        or suite_phase < 4
        or declared_phase is not None and not suite_id.startswith(f"p{suite_phase}-")
    ):
        raise ValueError("suite phase 与 suite ID 不匹配")
    contract = _suite_contract(suite_id, suite)
    environment = {
        "runner_environment": "github-hosted",
        "runner_os": platform.system(),
        "python": platform.python_version(),
    }
    plan: dict[str, object] = {
        "project_id": policy["project_id"],
        "requirement_id": policy["requirement_id"],
        "phase": suite_phase,
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
            contract, candidate_root, candidate_sha=candidate_sha, candidate_user=candidate_user,
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
        "phase": suite_phase,
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
