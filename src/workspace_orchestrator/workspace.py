"""面向人类可读需求工作区的本地优先持久化。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import RequirementStatus, WorkflowComplexity

WORKSPACE_FILES = (
    "requirement.md",
    "intent.md",
    "state.md",
    "plan.md",
    "decisions.md",
    "verification.md",
    "handoff.md",
)

SECTION_ALIASES = {
    "目标": "Goal",
    "目的": "Purpose",
    "背景": "Background",
    "范围": "Scope",
    "非目标": "Non-goals",
    "验收标准": "Acceptance Criteria",
    "原因": "Why",
    "期望结果": "Desired Outcome",
    "不得演变成": "Must Not Become",
    "设计方向": "Design Direction",
    "约束": "Constraints",
    "取舍优先级": "Trade-off Priorities",
    "执行优先级": "Execution Priority",
    "意图审查": "Intent Review",
    "阶段": "Phase",
    "已完成": "Completed",
    "进行中": "In Progress",
    "待处理": "Pending",
    "已阻塞": "Blocked",
    "下一步行动": "Next Action",
    "审查反馈": "Review Feedback",
    "单元测试": "Unit Tests",
    "类型检查": "Type Check",
    "集成测试": "Integration Tests",
    "最新检查": "Latest Check",
    "上次会话": "Last Session",
    "已修改文件": "Files Changed",
    "当前状态": "Current State",
    "重要上下文": "Important Context",
    "建议的下一步行动": "Next Recommended Action",
    "已知问题": "Known Problems",
}
SECTION_LABELS = {canonical: chinese for chinese, canonical in SECTION_ALIASES.items()}


class WorkspaceError(RuntimeError):
    """持久化工作区状态缺失或无效时抛出。"""


_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_STATE = threading.local()
_PROJECT_DEFAULT = object()


@contextmanager
def _file_lock(path: Path, *, timeout: float = 10.0):
    """提供同进程可重入、进程退出后自动释放的跨进程文件锁。"""

    # 不根据锁文件当前是否存在重新解析路径；Windows 对“创建前/创建后”的
    # resolve 结果可能采用不同大小写，进而破坏同线程嵌套更新的重入识别。
    key = os.path.normcase(os.path.abspath(path))
    with _LOCK_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        counts = getattr(_LOCK_STATE, "counts", None)
        if counts is None:
            counts = {}
            _LOCK_STATE.counts = counts
        if counts.get(key, 0):
            counts[key] += 1
            try:
                yield
            finally:
                counts[key] -= 1
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR)
        if os.path.getsize(path) == 0:
            os.write(descriptor, b"0")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise WorkspaceError(f"等待工作区文件锁超时：{path}")
                time.sleep(0.02)
        counts[key] = 1
        try:
            yield
        finally:
            counts.pop(key, None)
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def markdown_sections(text: str) -> dict[str, str]:
    """返回二级 Markdown 章节，并兼容中英文标题。"""

    matches = list(re.finditer(r"(?m)^## ([^\r\n]+)\s*$", text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = match.group(1).strip()
        body = text[match.end() : end].strip()
        sections[SECTION_ALIASES.get(heading, heading)] = body
    return sections


def replace_section(text: str, heading: str, body: str) -> str:
    """替换或追加一个二级 Markdown 章节，同时保留原有标题语言。"""

    labels = (heading, SECTION_LABELS.get(heading, heading))
    alternatives = "|".join(re.escape(label) for label in dict.fromkeys(labels))
    pattern = re.compile(
        rf"(?ms)^## (?P<label>{alternatives})\s*$.*?(?=^## |\Z)",
    )
    display_heading = SECTION_LABELS.get(heading, heading)
    if pattern.search(text):
        return (
            pattern.sub(
                lambda match: f"## {match.group('label')}\n\n{body.strip()}\n\n",
                text,
                count=1,
            ).rstrip()
            + "\n"
        )
    replacement = f"## {display_heading}\n\n{body.strip()}\n\n"
    return text.rstrip() + "\n\n" + replacement


def bullets(value: str) -> list[str]:
    return [
        line.removeprefix("- ").strip()
        for line in value.splitlines()
        if line.strip().startswith("- ") and line.removeprefix("- ").strip()
    ]


def initial_intent(title: str, goal: str | None = None, *, migrated: bool = False) -> str:
    why = (
        "迁移到意图层之前没有记录意图；请在审查前补充说明。" if migrated else "说明此需求为何重要。"
    )
    return (
        "# 需求意图\n\n"
        f"## 原因\n\n{why}\n\n"
        f"## 期望结果\n\n{goal or title}\n\n"
        "## 设计方向\n\n使用满足需求的最简单安全方案。\n\n"
        "## 约束\n\n保留项目意图和用户意图。\n\n"
        "## 非目标\n\n不得扩展到需求范围之外。\n\n"
        "## 取舍优先级\n\n实用价值优先，其次是简单性，最后是可扩展性。\n\n"
        "## 意图审查\n\n"
        "- 用户原则：PARTIAL\n"
        "- 项目意图：PARTIAL\n"
        "- 需求意图：PARTIAL\n"
        "- 不必要的复杂度：PARTIAL\n\n"
        "证据：\n\n尚未审查。\n"
    )


@dataclass(slots=True)
class WorkspaceStore:
    """管理 `.workspace/REQ-*` 目录的文件系统仓库。"""

    project_root: Path
    execution_root: Path | None = None

    @property
    def working_root(self) -> Path:
        """返回当前执行工作树；持久状态仍由 project_root 持有。"""

        return self.execution_root or self.project_root

    @property
    def root(self) -> Path:
        return self.project_root / ".workspace"

    def path_for(self, requirement_id: str) -> Path:
        normalized = requirement_id.upper()
        if not re.fullmatch(r"REQ-\d{3,}", normalized):
            raise WorkspaceError(f"无效的需求 ID：{requirement_id}")
        return self.root / normalized

    @contextmanager
    def locked(self, requirement_id: str | None = None):
        """锁定整个 Workspace 或单个 Requirement 的复合更新。"""

        name = ".workspace.lock" if requirement_id is None else f".{requirement_id.upper()}.lock"
        with _file_lock(self.root / name):
            yield

    @contextmanager
    def provider_locked(self, requirement_id: str):
        """串行同一 Requirement 的 Review Provider 同步，不扩大本地 RMW 临界区。"""

        with _file_lock(self.root / f".{requirement_id.upper()}.provider.lock"):
            yield

    @contextmanager
    def finalize_locked(self, requirement_id: str):
        """串行同一 Requirement 的完整 finalize；进程退出时由 OS 自动释放。"""

        with _file_lock(self.root / f".{requirement_id.upper()}.finalize.lock"):
            yield

    def next_id(self) -> str:
        existing = []
        if self.root.exists():
            for path in self.root.iterdir():
                match = re.fullmatch(r"REQ-(\d+)", path.name)
                if path.is_dir() and match:
                    existing.append(int(match.group(1)))
        return f"REQ-{max(existing, default=0) + 1:03d}"

    def current_id(self) -> str:
        """解析唯一的活动需求，不进行静默猜测。"""

        active: list[str] = []
        if self.root.exists():
            for path in sorted(self.root.iterdir()):
                if not path.is_dir() or not re.fullmatch(r"REQ-\d{3,}", path.name):
                    continue
                meta_path = path / "meta.json"
                if meta_path.is_file() and self.read_json(meta_path).get("status") in {
                    "draft",
                    "ready",
                    "in_progress",
                    "blocked",
                }:
                    active.append(path.name)
        if not active:
            raise WorkspaceError("未找到活动的需求工作区")
        if len(active) > 1:
            raise WorkspaceError("找到多个活动的需求工作区，请指定其中一个：" + ", ".join(active))
        return active[0]

    def requirement_ids(self, *, statuses: set[str] | None = None) -> tuple[str, ...]:
        """按 ID 返回完整 Requirement 列表，可按本地状态过滤。"""

        result: list[str] = []
        if not self.root.exists():
            return ()
        for path in sorted(self.root.iterdir()):
            if not path.is_dir() or not re.fullmatch(r"REQ-\d{3,}", path.name):
                continue
            meta_path = path / "meta.json"
            if not meta_path.is_file():
                continue
            if statuses is None or self.read_json(meta_path).get("status") in statuses:
                result.append(path.name)
        return tuple(result)

    def requirement_id_for_session(
        self,
        session_id: str,
        *,
        results: set[str] | None = None,
    ) -> str | None:
        """按允许的 Session 结果查找需求，不容忍一对多歧义。"""

        selected_results = results or {"in_progress"}
        attached: list[str] = []
        if self.root.exists():
            for path in sorted(self.root.iterdir()):
                if not path.is_dir() or not re.fullmatch(r"REQ-\d{3,}", path.name):
                    continue
                sessions_path = path / "sessions.json"
                if not sessions_path.is_file():
                    continue
                sessions = self.read_json(sessions_path)
                if any(
                    item.get("id") == session_id and item.get("result") in selected_results
                    for item in sessions
                ):
                    attached.append(path.name)
        if len(attached) > 1:
            raise WorkspaceError(f"会话 {session_id} 同时关联了多个需求：" + ", ".join(attached))
        return attached[0] if attached else None

    def attached_requirement_id(self, session_id: str) -> str | None:
        """查找当前活动绑定的需求。"""

        return self.requirement_id_for_session(session_id)

    def active_session_conflicts(self) -> dict[str, tuple[str, ...]]:
        """返回活跃 Session 的多需求绑定；仅诊断，不修改任何状态。"""

        bindings: dict[str, list[str]] = {}
        for requirement_id in self.requirement_ids():
            sessions_path = self.path_for(requirement_id) / "sessions.json"
            if not sessions_path.is_file():
                continue
            for session in self.read_json(sessions_path):
                session_id = str(session.get("id") or "").strip()
                if session_id and session.get("result") == "in_progress":
                    bindings.setdefault(session_id, []).append(requirement_id)
        return {
            session_id: tuple(requirements)
            for session_id, requirements in bindings.items()
            if len(requirements) > 1
        }

    def create(
        self,
        title: str,
        *,
        goal: str | None = None,
        acceptance: list[str] | None = None,
        complexity: WorkflowComplexity = WorkflowComplexity.NORMAL,
        task_provider: str | None | object = _PROJECT_DEFAULT,
        task_project_id: str | None = None,
        manual_test_required: bool = False,
        creation_key: str | None = None,
    ) -> str:
        from .project_config import default_project_config, load_project_config

        project_config = (
            load_project_config(self.working_root)
            or load_project_config(self.project_root)
            or default_project_config(self.project_root)
        )
        selected_provider = (
            project_config.task_provider if task_provider is _PROJECT_DEFAULT else task_provider
        )
        if selected_provider == "dashi":
            selected_project_id = (
                task_project_id
                or project_config.task_project_id
                or default_project_config(self.project_root).task_project_id
            )
        else:
            selected_project_id = task_project_id
        with self.locked():
            normalized_creation_key = (creation_key or "").strip() or None
            if normalized_creation_key and self.root.exists():
                for existing_path in sorted(self.root.iterdir()):
                    meta_path = existing_path / "meta.json"
                    if (
                        existing_path.is_dir()
                        and re.fullmatch(r"REQ-\d{3,}", existing_path.name)
                        and meta_path.is_file()
                        and self.read_json(meta_path).get("creation_key")
                        == normalized_creation_key
                    ):
                        return existing_path.name
            requirement_id = self.next_id()
            path = self.path_for(requirement_id)
            path.mkdir(parents=True, exist_ok=False)
            timestamp = now_iso()
            meta = {
            "id": requirement_id,
            "title": title,
            "status": RequirementStatus.DRAFT.value,
            "complexity": complexity.value,
            "workflow": complexity.value,
            "created_at": timestamp,
            "updated_at": timestamp,
            "task_provider": selected_provider,
            "task_project_id": selected_project_id,
            "task_provider_explicitly_disabled": selected_provider is None,
            "manual_test_required": manual_test_required,
            "agent_provider": "codex",
            "git": {"branch": None, "worktree": None},
            }
            if normalized_creation_key:
                meta["creation_key"] = normalized_creation_key
            self.write_json(path / "meta.json", meta)
            criteria = acceptance or ["定义验收标准"]
            checked = "\n".join(f"- [ ] {item}" for item in criteria)
            self.write_text(
            path / "requirement.md",
            "# 需求\n\n"
            f"## 目标\n\n{goal or title}\n\n"
            "## 背景\n\n\n\n## 范围\n\n\n\n## 非目标\n\n\n\n"
            f"## 验收标准\n\n{checked}\n",
            )
            self.write_text(path / "intent.md", initial_intent(title, goal))
            self.write_text(
            path / "state.md",
            "# 状态\n\n## 阶段\n\ndraft（草稿）\n\n## 已完成\n\n无\n\n"
            "## 进行中\n\n无\n\n## 待处理\n\n- 定义范围和计划\n\n"
            "## 已阻塞\n\n无\n\n## 下一步行动\n\n定义范围和验收标准。\n",
            )
            self.write_text(path / "plan.md", "# 计划\n\n- [ ] 定义范围和计划\n")
            self.write_text(path / "decisions.md", "# 决策\n\n尚未记录决策。\n")
            self.write_text(
            path / "verification.md",
            "# 验证\n\n## 单元测试\n\n状态：TODO\n\n"
            "## 类型检查\n\n状态：TODO\n\n## 集成测试\n\n状态：TODO\n",
            )
            self.write_text(
            path / "handoff.md",
            "# 交接\n\n## 上次会话\n\n无\n\n## 已完成\n\n无\n\n"
            "## 已修改文件\n\n无\n\n## 当前状态\n\n工作区已创建。\n\n"
            "## 重要上下文\n\n无\n\n## 建议的下一步行动\n\n"
            "定义范围和验收标准。\n\n## 已知问题\n\n无\n",
            )
            self.write_json(path / "sessions.json", [])
        return requirement_id

    def load(self, requirement_id: str) -> dict[str, Any]:
        path = self.path_for(requirement_id)
        if not path.is_dir():
            raise WorkspaceError(f"未找到工作区：{requirement_id}")
        legacy_files = tuple(name for name in WORKSPACE_FILES if name != "intent.md")
        missing = [
            name
            for name in ("meta.json", *legacy_files, "sessions.json")
            if not (path / name).is_file()
        ]
        if missing:
            raise WorkspaceError(f"工作区 {requirement_id} 不完整：{', '.join(missing)}")
        from .project_config import default_project_config, load_project_config

        with self.locked(requirement_id):
            meta_path = path / "meta.json"
            meta = self.read_json(meta_path)
            if (
                meta.get("task_provider") is None
                and "task_provider_explicitly_disabled" not in meta
            ):
                config = (
                    load_project_config(self.working_root)
                    or load_project_config(self.project_root)
                    or default_project_config(self.project_root)
                )
                meta["task_provider"] = config.task_provider
                meta["task_project_id"] = config.task_project_id
                meta["task_provider_explicitly_disabled"] = False
                meta["task_provider_migrated_from_legacy"] = True
                meta["updated_at"] = now_iso()
                self.write_json(meta_path, meta)
            elif meta.get("task_provider_migrated_from_legacy") or (
                meta.get("task_provider_explicitly_disabled") is False
                and meta.get("task_project_id")
                == default_project_config(self.project_root).task_project_id
            ):
                config = load_project_config(self.working_root) or load_project_config(
                    self.project_root
                )
                if config and (
                    meta.get("task_provider") != config.task_provider
                    or meta.get("task_project_id") != config.task_project_id
                ):
                    meta["task_provider"] = config.task_provider
                    meta["task_project_id"] = config.task_project_id
                    meta.pop("requirement_review_task_id", None)
                    meta.pop("review_comment_count", None)
                    meta["updated_at"] = now_iso()
                    self.write_json(meta_path, meta)
        if not (path / "intent.md").is_file():
            meta = self.read_json(path / "meta.json")
            requirement = markdown_sections((path / "requirement.md").read_text(encoding="utf-8"))
            self.write_text(
                path / "intent.md",
                initial_intent(
                    str(meta.get("title") or requirement_id),
                    requirement.get("Goal"),
                    migrated=True,
                ),
            )
        return {
            "path": path,
            "meta": self.read_json(path / "meta.json"),
            "sessions": self.read_json(path / "sessions.json"),
            **{
                name.removesuffix(".md"): (path / name).read_text(encoding="utf-8")
                for name in WORKSPACE_FILES
            },
        }

    def touch_meta(self, requirement_id: str, **changes: object) -> dict[str, Any]:
        with self.locked(requirement_id):
            path = self.path_for(requirement_id) / "meta.json"
            meta = self.read_json(path)
            meta.update(changes)
            meta["updated_at"] = now_iso()
            self.write_json(path, meta)
            return meta

    @staticmethod
    def read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"无法读取 {path}：{exc}") from exc

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        WorkspaceStore.write_text(path, json.dumps(value, ensure_ascii=False, indent=2))

    @staticmethod
    def write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(value.rstrip() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
