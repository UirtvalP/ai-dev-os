"""精简的意图恢复与可审计的意图一致性审查。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .workspace import SECTION_LABELS, markdown_sections


class IntentStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    VIOLATION = "VIOLATION"


INTENT_CHECKS = (
    "User Principles",
    "Project Intent",
    "Requirement Intent",
    "Unnecessary Complexity",
)
INTENT_CHECK_ALIASES = {
    "用户原则": "User Principles",
    "项目意图": "Project Intent",
    "需求意图": "Requirement Intent",
    "不必要的复杂度": "Unnecessary Complexity",
}
INTENT_CHECK_LABELS = {canonical: chinese for chinese, canonical in INTENT_CHECK_ALIASES.items()}


@dataclass(frozen=True, slots=True)
class IntentReview:
    status: IntentStatus
    checks: tuple[tuple[str, IntentStatus], ...]

    @property
    def incomplete(self) -> tuple[str, ...]:
        return tuple(name for name, status in self.checks if status is not IntentStatus.PASS)


def review_intent(intent_document: str) -> IntentReview:
    """根据四项必需的持久化检查推导总体状态。"""

    section = markdown_sections(intent_document).get("Intent Review", "")
    parsed = {}
    for name, status in re.findall(
        r"(?im)^-\s*([^:：\r\n]+)[:：]\s*(PASS|PARTIAL|VIOLATION)\s*$", section
    ):
        normalized_name = INTENT_CHECK_ALIASES.get(name.strip(), name.strip())
        parsed[normalized_name] = IntentStatus(status.upper())
    checks = tuple(
        (name, parsed.get(name, IntentStatus.PARTIAL)) for name in INTENT_CHECKS
    )
    statuses = {status for _, status in checks}
    if IntentStatus.VIOLATION in statuses:
        overall = IntentStatus.VIOLATION
    elif statuses == {IntentStatus.PASS}:
        overall = IntentStatus.PASS
    else:
        overall = IntentStatus.PARTIAL
    return IntentReview(overall, checks)


def _summary_line(body: str) -> str | None:
    match = re.search(r"(?im)^(?:Summary|摘要)[:：]\s*(.+)$", body)
    if match:
        return match.group(1).strip()
    for line in body.splitlines():
        value = re.sub(r"^(?:[-*]|\d+\.)\s+", "", line.strip())
        if value and not value.startswith("#"):
            return value
    return None


def summarize_document(
    path: Path,
    *,
    headings: tuple[str, ...] | None = None,
    limit: int = 4,
) -> tuple[str, ...]:
    """提取已声明的简短摘要，不复制完整意图文档。"""

    if not path.is_file():
        return (f"未定义（缺少 {path.name}）",)
    sections = markdown_sections(path.read_text(encoding="utf-8"))
    selected = headings or tuple(sections)
    summaries = []
    for heading in selected:
        if heading not in sections:
            continue
        summary = _summary_line(sections[heading])
        if summary:
            summaries.append(f"{SECTION_LABELS.get(heading, heading)}：{summary}")
        if len(summaries) >= limit:
            break
    return tuple(summaries) or (f"未定义（{path.name} 中没有摘要）",)


def requirement_intent_summary(intent_document: str) -> tuple[str, ...]:
    sections = markdown_sections(intent_document)
    summaries = []
    for heading in (
        "Why",
        "Desired Outcome",
        "Design Direction",
        "Constraints",
        "Trade-off Priorities",
    ):
        summary = _summary_line(sections.get(heading, ""))
        if summary:
            summaries.append(f"{SECTION_LABELS.get(heading, heading)}：{summary}")
    return tuple(summaries[:4]) or ("未定义",)
