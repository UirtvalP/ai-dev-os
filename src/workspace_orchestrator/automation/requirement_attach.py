"""项目发现与 Requirement 的确定性选择规则。"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from workspace_orchestrator.workspace import WorkspaceError, WorkspaceStore


class AutomationAmbiguity(WorkspaceError):
    """只有需要用户选择时才返回的结构化歧义。"""

    status = "ambiguity"


@dataclass(frozen=True, slots=True)
class NewRequirementRequest:
    """由明确自然语言指令解析出的新 Requirement 参数。"""

    title: str
    goal: str
    manual_test_required: bool = False


_NEW_REQUIREMENT = re.compile(
    r"^\s*(?:请\s*)?(?:新增|新建|创建|开\s*(?:一个|个)?)\s*"
    r"(?:一个|个)?\s*(?:新(?:的)?)?\s*(?:需求|requirement)\s*[:：,，。;；-]*\s*",
    re.IGNORECASE,
)
_NEGATED_NEW_REQUIREMENT = re.compile(
    r"^\s*(?:请\s*)?(?:不要|不用|无需|不必|别)\s*(?:新增|新建|创建|开)",
    re.IGNORECASE,
)
_MANUAL_TEST_REQUEST = re.compile(
    r"(?:这(?:个|次)?(?:需求)?\s*)?(?:需要|要求|请)\s*(?:进行\s*)?"
    r"(?:人工|手动)\s*(?:测试|验收)|"
    r"(?:人工|手动)\s*(?:测试|验收)(?:通过)?(?:后)?\s*(?:再|才)\s*(?:完成|结束)",
    re.IGNORECASE,
)
_MANUAL_TEST_EXCLUSION = re.compile(
    r"(?:不需要|无需|不要|不用|不必|除非)[^。；;\r\n]{0,24}"
    r"(?:人工|手动)\s*(?:测试|验收)",
    re.IGNORECASE,
)


def parse_new_requirement_request(prompt: str | None) -> NewRequirementRequest | None:
    """只识别位于请求开头的明确新增指令，普通修改与否定表达不创建。"""

    text = (prompt or "").strip()
    if not text or _NEGATED_NEW_REQUIREMENT.match(text):
        return None
    match = _NEW_REQUIREMENT.match(text)
    if match is None:
        return None
    goal = text[match.end() :].strip() or "新增需求"
    first_clause = re.split(r"[。；;\r\n]", goal, maxsplit=1)[0].strip(" ，,：:")
    title = first_clause or "新增需求"
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    positive_manual_test_text = _MANUAL_TEST_EXCLUSION.sub("", goal)
    return NewRequirementRequest(
        title=title,
        goal=goal,
        manual_test_required=bool(_MANUAL_TEST_REQUEST.search(positive_manual_test_text)),
    )


def discover_project_root(start: Path) -> Path:
    """发现共享 Workspace；关联 worktree 回退到 Git 主工作树。"""

    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".workspace").is_dir():
            return candidate
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=resolved,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=resolved,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError:
        return resolved
    if common.returncode == 0:
        common_dir = Path(common.stdout.strip()).resolve()
        main_root = common_dir.parent if common_dir.name == ".git" else None
        if main_root is not None and (main_root / ".workspace").is_dir():
            return main_root
    return Path(top.stdout.strip()).resolve() if top.returncode == 0 else resolved


def select_requirement(
    store: WorkspaceStore,
    session_id: str,
    explicit_requirement_id: str | None = None,
) -> tuple[str, str | None]:
    """按显式 ID、已有绑定、唯一活动 Requirement 的顺序选择。"""

    attached_id = store.attached_requirement_id(session_id)
    if explicit_requirement_id:
        selected_id = explicit_requirement_id.upper()
        store.load(selected_id)
        return selected_id, attached_id
    if attached_id:
        return attached_id, attached_id
    try:
        return store.current_id(), None
    except WorkspaceError as exc:
        if "多个活动" in str(exc):
            raise AutomationAmbiguity(f"ambiguity：{exc}") from exc
        raise
