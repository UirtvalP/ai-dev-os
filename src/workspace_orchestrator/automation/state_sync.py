"""Context Snapshot、checkpoint、handoff 与已知验证命令的状态同步。"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace_orchestrator.adapters.base import TaskProvider
from workspace_orchestrator.adapters.task import TaskProviderError
from workspace_orchestrator.intent import requirement_intent_summary, summarize_document
from workspace_orchestrator.workspace import (
    SECTION_LABELS,
    WorkspaceError,
    WorkspaceStore,
    bullets,
    markdown_sections,
    replace_section,
)

from .git_sync import collect_git_context


def _summary(value: str, fallback: str = "无") -> str:
    value = value.strip()
    return value if value else fallback


def _display_state(value: str) -> str:
    labels = {
        "draft": "草稿",
        "ready": "就绪",
        "in_progress": "进行中",
        "in_review": "审查中",
        "done": "已完成",
        "todo": "待处理",
        "blocked": "已阻塞",
        "tiny": "微型",
        "normal": "常规",
        "complex": "复杂",
        "research": "研究",
        "implementation": "实现",
    }
    label = labels.get(value)
    return f"{value}（{label}）" if label else value


def collect_snapshot(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    tasks: Sequence[object] = (),
    task_error: str | None = None,
    git: dict[str, Any] | None = None,
) -> str:
    """只从结构化事实构建 Snapshot，不注册 Session、不调用 LLM。"""

    data = store.load(requirement_id)
    meta = data["meta"]
    requirement = markdown_sections(data["requirement"])
    user_principles = summarize_document(store.working_root / "USER_PRINCIPLES.md")
    project_intent = summarize_document(
        store.working_root / "PROJECT_INTENT.md",
        headings=(
            "Purpose",
            "Desired Outcome",
            "Must Not Become",
            "Trade-off Priorities",
            "Execution Priority",
        ),
    )
    requirement_intent = requirement_intent_summary(data["intent"])
    state = markdown_sections(data["state"])
    handoff = markdown_sections(data["handoff"])
    verification = markdown_sections(data["verification"])
    stored_git = dict(meta.get("git") or {})
    git = git or collect_git_context(
        store.project_root, stored_git, execution_root=store.working_root
    )
    stored_git.update({key: git[key] for key in ("branch", "worktree") if git.get(key)})
    if stored_git != meta.get("git"):
        meta = store.touch_meta(requirement_id, git=stored_git)

    if task_error:
        task_lines = [f"不可用（{task_error}）"]
    else:
        task_lines = [
            f"- {task.id} [{_display_state(task.status)}] {task.title}" for task in tasks
        ] or ["无"]
    completed = bullets(state.get("Completed", ""))
    pending = bullets(state.get("Pending", ""))
    decisions_text = data["decisions"].removeprefix("# Decisions").removeprefix("# 决策").strip()
    verification_lines = []
    for name, body in verification.items():
        status_line = next(
            (
                line.strip()
                for line in body.splitlines()
                if "Status:" in line or "状态：" in line or "状态:" in line
            ),
            body,
        )
        verification_lines.append(f"- {SECTION_LABELS.get(name, name)}：{_summary(status_line)}")
    git_status = git.get("status")
    if git.get("error"):
        git_status = f"不可用（{git['error']}）"
    parts = [
        "# 工作区上下文",
        f"需求：\n{meta['id']} {meta['title']}",
        f"目标：\n{_summary(requirement.get('Goal', ''))}",
        "用户原则：\n" + "\n".join(f"- {item}" for item in user_principles),
        "项目意图：\n" + "\n".join(f"- {item}" for item in project_intent),
        "需求意图：\n" + "\n".join(f"- {item}" for item in requirement_intent),
        f"状态：\n{_display_state(meta['status'])}",
        f"工作流：\n{_display_state(meta['workflow'])}",
        f"当前阶段：\n{_display_state(_summary(state.get('Phase', '')))}",
        "任务：\n" + "\n".join(task_lines),
        "已完成：\n" + ("\n".join(f"- {item}" for item in completed) or "无"),
        "待处理：\n" + ("\n".join(f"- {item}" for item in pending) or "无"),
        f"重要决策：\n{_summary(decisions_text)}",
        (
            "Git：\n"
            f"- 分支：{_summary(str(stored_git.get('branch') or ''))}\n"
            f"- 工作树：{_summary(str(stored_git.get('worktree') or ''))}\n"
            f"- 状态：{_summary(str(git_status or ''), '干净')}\n"
            "- 最近提交：\n"
            + ("\n".join(f"  - {item}" for item in git.get("commits", ())) or "  无")
        ),
        "验证：\n" + ("\n".join(verification_lines) or "无"),
        f"上次交接：\n{_summary(handoff.get('Current State', ''))}",
        f"下一步行动：\n{_summary(state.get('Next Action', '') or handoff.get('Next Recommended Action', ''))}",
    ]
    return "\n\n".join(parts).rstrip() + "\n"


def persist_checkpoint(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    phase: str | None = None,
    completed: Sequence[str] = (),
    next_action: str | None = None,
    verification: str | None = None,
) -> None:
    """幂等写入本地 checkpoint 状态。"""

    with store.locked(requirement_id):
        data = store.load(requirement_id)
        state = data["state"]
        if phase:
            state = replace_section(state, "Phase", phase)
        if completed:
            current = bullets(markdown_sections(state).get("Completed", ""))
            merged = list(dict.fromkeys([*current, *completed]))
            state = replace_section(state, "Completed", "\n".join(f"- {item}" for item in merged))
        if next_action:
            state = replace_section(state, "Next Action", next_action)
        store.write_text(data["path"] / "state.md", state)
        if completed:
            plan = data["plan"]
            for item in completed:
                unchecked = f"- [ ] {item}"
                in_progress = f"- [-] {item}"
                if unchecked in plan:
                    plan = plan.replace(unchecked, f"- [x] {item}", 1)
                elif in_progress in plan:
                    plan = plan.replace(in_progress, f"- [x] {item}", 1)
                elif f"- [x] {item}" not in plan:
                    plan = plan.rstrip() + f"\n- [x] {item}\n"
            store.write_text(data["path"] / "plan.md", plan)
        if verification:
            verification_doc = replace_section(data["verification"], "Latest Check", verification)
            store.write_text(data["path"] / "verification.md", verification_doc)
        status = data["meta"]["status"]
        # 纯 handoff/等待确认文案不改变审查证据；阶段、完成项或验证变化才使旧审查失效。
        changed = bool(phase or completed or verification)
        if (
            status in {"draft", "ready"}
            and phase
            and phase not in {"draft", "ready"}
            or status == "in_review"
            and changed
            and phase != "review"
        ):
            status = "in_progress"
        store.touch_meta(requirement_id, status=status)


def persist_handoff(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    session_id: str,
    completed: Sequence[str] = (),
    files_changed: Sequence[str] = (),
    current_state: str | None = None,
    important_context: str | None = None,
    next_action: str | None = None,
    known_problems: str | None = None,
) -> None:
    with store.locked(requirement_id):
        data = store.load(requirement_id)
        state = markdown_sections(data["state"])
        previous_handoff = markdown_sections(data["handoff"])
        doc = data["handoff"]
        merged_files = list(
            dict.fromkeys(
                [*bullets(previous_handoff.get("Files Changed", "")), *files_changed]
            )
        )
        fields = {
            "Last Session": session_id,
            "Completed": "\n".join(f"- {item}" for item in completed)
            or state.get("Completed", "无"),
            "Files Changed": "\n".join(f"- {item}" for item in merged_files) or "无",
            "Current State": current_state or state.get("In Progress", "无"),
            "Important Context": important_context or "无",
            "Next Recommended Action": next_action or state.get("Next Action", "无"),
            "Known Problems": known_problems or "无",
        }
        for heading, value in fields.items():
            doc = replace_section(doc, heading, value)
        store.write_text(data["path"] / "handoff.md", doc)
        store.touch_meta(requirement_id)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    command: tuple[str, ...]
    returncode: int
    output: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def _verification_config(project_root: Path) -> tuple[tuple[tuple[str, ...], ...], float]:
    """读取并严格校验验证命令与统一超时。"""

    config_path = project_root / "pyproject.toml"
    if not config_path.is_file():
        return (), 300.0
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkspaceError(f"验证配置无效：{exc}") from exc
    configured = (
        config.get("tool", {})
        .get("workspace-orchestrator", {})
        .get("automation", {})
        .get("verification-commands", ())
    )
    timeout = (
        config.get("tool", {})
        .get("workspace-orchestrator", {})
        .get("automation", {})
        .get("verification-timeout-seconds", 300)
    )
    if not isinstance(configured, list):
        raise WorkspaceError("验证配置无效：verification-commands 必须是数组")
    commands: list[tuple[str, ...]] = []
    for index, command in enumerate(configured, start=1):
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part.strip() for part in command)
        ):
            raise WorkspaceError(
                f"验证配置无效：verification-commands 第 {index} 项必须是非空字符串数组"
            )
        commands.append(tuple(part.replace("{python}", sys.executable) for part in command))
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 0 < timeout <= 3600
    ):
        raise WorkspaceError("验证配置无效：verification-timeout-seconds 必须在 0 到 3600 之间")
    return tuple(commands), float(timeout)


def known_verification_commands(project_root: Path) -> tuple[tuple[str, ...], ...]:
    """从项目配置读取命令；不存在配置时不猜测。"""

    return _verification_config(project_root)[0]


def run_known_verifications(project_root: Path) -> tuple[VerificationResult, ...]:
    commands, timeout = _verification_config(project_root)
    results = []
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            output = "\n".join(
                part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
            )
            result = VerificationResult(command, completed.returncode, output)
        except subprocess.TimeoutExpired:
            result = VerificationResult(
                command,
                124,
                f"命令超过 {timeout:g} 秒超时",
                timed_out=True,
            )
        except OSError as exc:
            result = VerificationResult(command, 127, f"无法启动命令：{exc}")
        results.append(result)
        if not result.passed:
            break
    return tuple(results)


def verification_summary(results: Sequence[VerificationResult]) -> str:
    if not results:
        return "状态：TODO - 未配置已知验证命令"
    lines = [
        f"状态：{'PASS' if all(item.passed for item in results) else 'FAIL'}",
        *(
            f"- {'PASS' if item.passed else 'FAIL'}：{' '.join(item.command)}"
            + (f"（{item.output}）" if not item.passed and item.output else "")
            for item in results
        ),
    ]
    return "\n".join(lines)


def persist_verification_results(
    store: WorkspaceStore,
    requirement_id: str,
    results: Sequence[VerificationResult],
) -> None:
    """把已知命令结果同步到既有三类人类可读验证章节。"""

    with store.locked(requirement_id):
        data = store.load(requirement_id)
        document = data["verification"]
        grouped: dict[str, list[VerificationResult]] = {}
        for result in results:
            command_text = " ".join(result.command).casefold()
            if "pytest" in command_text:
                section = "Unit Tests"
            elif "ruff" in command_text or "mypy" in command_text or "pyright" in command_text:
                section = "Type Check"
            else:
                section = "Integration Tests"
            grouped.setdefault(section, []).append(result)
        for section, section_results in grouped.items():
            body = [
                "命令：",
                *[f"- {' '.join(item.command)}" for item in section_results],
                "",
                f"状态：{'PASS' if all(item.passed for item in section_results) else 'FAIL'}",
            ]
            document = replace_section(document, section, "\n".join(body))
        store.write_text(data["path"] / "verification.md", document)


def list_tasks_safely(
    task_provider: TaskProvider | None, requirement_id: str
) -> tuple[tuple[object, ...], str | None]:
    if task_provider is None:
        return (), None
    try:
        return tuple(task_provider.list_tasks(requirement_id)), None
    except TaskProviderError as exc:
        return (), str(exc)
