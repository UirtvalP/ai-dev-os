"""从 Workspace 事实确定性生成可独立验收的 Review Packet。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .intent import review_intent
from .models import Task
from .workspace import WorkspaceStore, bullets, markdown_sections

PACKET_SCHEMA_VERSION = 1
_MARKER = re.compile(
    r"<!-- ai-dev-os-review-packet:v1 requirement=(REQ-\d+) revision=(\d+) "
    r"fingerprint=sha256:([0-9a-f]{64}) -->"
)


@dataclass(frozen=True, slots=True)
class ReviewCriterion:
    text: str
    passed: bool


@dataclass(frozen=True, slots=True)
class ReviewVerification:
    name: str
    commands: tuple[str, ...]
    status: str
    result: str


@dataclass(frozen=True, slots=True)
class ReviewTaskEvidence:
    id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class ReviewPacket:
    schema_version: int
    requirement_id: str
    title: str
    goal: str
    scope: tuple[str, ...]
    completed: tuple[str, ...]
    acceptance: tuple[ReviewCriterion, ...]
    intent_status: str
    verification: tuple[ReviewVerification, ...]
    changed_files: tuple[str, ...]
    branch: str
    worktree: str
    commits: tuple[str, ...]
    diff_context: str
    known_risks: tuple[str, ...]
    development_tasks: tuple[ReviewTaskEvidence, ...]

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_items(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip() and value != "无"))


def build_review_packet(
    store: WorkspaceStore,
    requirement_id: str,
    *,
    tasks: Sequence[Task] = (),
    git: dict[str, Any] | None = None,
) -> ReviewPacket:
    data = store.load(requirement_id)
    requirement = markdown_sections(data["requirement"])
    state = markdown_sections(data["state"])
    handoff = markdown_sections(data["handoff"])
    verification_doc = markdown_sections(data["verification"])
    criteria = tuple(
        ReviewCriterion(text.strip(), mark.casefold() == "x")
        for mark, text in re.findall(
            r"(?m)^- \[([ xX])\] (.+)$", requirement.get("Acceptance Criteria", "")
        )
    )
    verification: list[ReviewVerification] = []
    for name, body in verification_doc.items():
        statuses = re.findall(r"(?im)^(?:Status|状态)[:：]\s*(\S+)", body)
        commands = tuple(
            line.removeprefix("- ").strip()
            for line in body.splitlines()
            if line.strip().startswith("- ")
        )
        results = re.findall(r"(?im)^(?:Result|结果)[:：]\s*(.+)$", body)
        result = results[-1].strip() if results else "未记录结果摘要"
        verification.append(
            ReviewVerification(name, commands, statuses[-1].upper() if statuses else "MISSING", result)
        )
    git = git or {}
    scope = _clean_items(bullets(requirement.get("Scope", "")))
    if not scope and requirement.get("Scope", "").strip():
        scope = (requirement["Scope"].strip(),)
    if not scope and requirement.get("Goal", "").strip():
        scope = ("以需求目标与验收标准为实现边界。",)
    completed = _clean_items(
        [*bullets(state.get("Completed", "")), *bullets(handoff.get("Completed", ""))]
    )
    changed_files = _clean_items(
        [*bullets(handoff.get("Files Changed", "")), *git.get("changed_files", ())]
    )
    risks = _clean_items(
        [*bullets(state.get("Blocked", "")), *bullets(handoff.get("Known Problems", ""))]
    ) or ("无",)
    development_tasks = tuple(
        ReviewTaskEvidence(task.id, task.title, task.status)
        for task in sorted(tasks, key=lambda item: item.id)
        if "requirement-review" not in task.labels
    )
    diff_context = str(git.get("diff") or git.get("status") or "当前工作树无未提交修改。")
    return ReviewPacket(
        PACKET_SCHEMA_VERSION,
        requirement_id.upper(),
        str(data["meta"].get("title") or requirement_id),
        requirement.get("Goal", "").strip(),
        scope,
        completed,
        criteria,
        str(review_intent(data["intent"]).status),
        tuple(verification),
        changed_files,
        str(git.get("branch") or ""),
        str(git.get("worktree") or ""),
        tuple(str(item) for item in git.get("commits", ())),
        diff_context[:8000],
        risks,
        development_tasks,
    )


def validate_review_packet(packet: ReviewPacket, *, git_error: str | None = None) -> tuple[str, ...]:
    blockers: list[str] = []
    if not packet.goal:
        blockers.append("Review Packet 缺少需求目标")
    if not packet.scope:
        blockers.append("Review Packet 缺少范围")
    if not packet.completed:
        blockers.append("Review Packet 缺少本次完成内容")
    if not packet.acceptance:
        blockers.append("Review Packet 缺少验收标准")
    blockers.extend(f"Review Packet 验收未通过：{item.text}" for item in packet.acceptance if not item.passed)
    if not packet.verification or not any(item.commands for item in packet.verification):
        blockers.append("Review Packet 缺少验证命令")
    blockers.extend(
        f"Review Packet 验证未通过：{item.name} [{item.status}]"
        for item in packet.verification
        if item.status != "PASS"
    )
    if packet.intent_status != "PASS":
        blockers.append(f"Review Packet 意图一致性未通过：{packet.intent_status}")
    if git_error:
        blockers.append(f"Review Packet Git 证据不可用：{git_error}")
    if not packet.worktree:
        blockers.append("Review Packet 缺少 Git worktree")
    return tuple(blockers)


def render_review_packet(packet: ReviewPacket, revision: int) -> str:
    fingerprint = packet.fingerprint

    def lines(values: Sequence[str], *, indent: str = "") -> str:
        return "\n".join(f"{indent}- {value}" for value in values) or f"{indent}无"

    acceptance = "\n".join(
        f"- [{'x' if item.passed else ' '}] {item.text}" for item in packet.acceptance
    ) or "无"
    verification = "\n".join(
        f"- {item.name}：{item.status}\n"
        + (lines(item.commands, indent="  ") if item.commands else "  无命令")
        + f"\n  结果：{item.result}"
        for item in packet.verification
    ) or "无"
    tasks = "\n".join(
        f"- {item.id} [{item.status}] {item.title}" for item in packet.development_tasks
    ) or "无"
    commits = lines(packet.commits, indent="  ")
    return (
        f"<!-- ai-dev-os-review-packet:v1 requirement={packet.requirement_id} "
        f"revision={revision} fingerprint=sha256:{fingerprint} -->\n\n"
        f"# Requirement Review：{packet.requirement_id} {packet.title}\n\n"
        f"## 需求目标与范围\n\n{packet.goal}\n\n{lines(packet.scope)}\n\n"
        f"## 本次完成内容\n\n{lines(packet.completed)}\n\n"
        f"## 验收标准及状态\n\n{acceptance}\n\n"
        f"## 意图一致性\n\n{packet.intent_status}\n\n"
        f"## 验证命令与结果\n\n{verification}\n\n"
        f"## 修改文件\n\n{lines(packet.changed_files)}\n\n"
        f"## Git 上下文\n\n- branch：{packet.branch or '无'}\n"
        f"- worktree：{packet.worktree or '无'}\n- 最近提交：\n{commits}\n\n"
        f"```text\n{packet.diff_context}\n```\n\n"
        f"## 已知问题与风险\n\n{lines(packet.known_risks)}\n\n"
        f"## 关联开发 Task\n\n{tasks}\n\n"
        "## 批准或退回\n\n"
        "批准：确认以上材料后，仅由用户把本 Review 卡移到 done。\n\n"
        "退回：在本卡留言明确修改意见，再把本 Review 卡移到 in_progress。\n\n"
        "普通开发 Task 的 done 不代表 Requirement 已获批准。\n"
    )


def parse_review_packet_marker(description: str) -> tuple[str, int, str] | None:
    match = _MARKER.search(description)
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)
