"""Local-first persistence for human-readable requirement workspaces."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import RequirementStatus, WorkflowComplexity

WORKSPACE_FILES = (
    "requirement.md",
    "state.md",
    "plan.md",
    "decisions.md",
    "verification.md",
    "handoff.md",
)


class WorkspaceError(RuntimeError):
    """Raised when persisted workspace state is missing or invalid."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def markdown_sections(text: str) -> dict[str, str]:
    """Return level-two Markdown sections without imposing a schema."""

    matches = list(re.finditer(r"(?m)^## ([^\r\n]+)\s*$", text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[match.end() : end].strip()
    return sections


def replace_section(text: str, heading: str, body: str) -> str:
    """Replace or append one level-two Markdown section."""

    pattern = re.compile(
        rf"(?ms)^## {re.escape(heading)}\s*$.*?(?=^## |\Z)",
    )
    replacement = f"## {heading}\n\n{body.strip()}\n\n"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1).rstrip() + "\n"
    return text.rstrip() + "\n\n" + replacement


def bullets(value: str) -> list[str]:
    return [
        line.removeprefix("- ").strip()
        for line in value.splitlines()
        if line.strip().startswith("- ") and line.removeprefix("- ").strip()
    ]


@dataclass(slots=True)
class WorkspaceStore:
    """Filesystem repository for `.workspace/REQ-*` directories."""

    project_root: Path

    @property
    def root(self) -> Path:
        return self.project_root / ".workspace"

    def path_for(self, requirement_id: str) -> Path:
        normalized = requirement_id.upper()
        if not re.fullmatch(r"REQ-\d{3,}", normalized):
            raise WorkspaceError(f"Invalid requirement ID: {requirement_id}")
        return self.root / normalized

    def next_id(self) -> str:
        existing = []
        if self.root.exists():
            for path in self.root.iterdir():
                match = re.fullmatch(r"REQ-(\d+)", path.name)
                if path.is_dir() and match:
                    existing.append(int(match.group(1)))
        return f"REQ-{max(existing, default=0) + 1:03d}"

    def current_id(self) -> str:
        """Resolve the only active Requirement without silently guessing."""

        active: list[str] = []
        if self.root.exists():
            for path in sorted(self.root.iterdir()):
                if not path.is_dir() or not re.fullmatch(r"REQ-\d{3,}", path.name):
                    continue
                meta_path = path / "meta.json"
                if meta_path.is_file() and self.read_json(meta_path).get("status") != "done":
                    active.append(path.name)
        if not active:
            raise WorkspaceError("No active Requirement Workspace found")
        if len(active) > 1:
            raise WorkspaceError(
                "Multiple active Requirement Workspaces found; specify one: " + ", ".join(active)
            )
        return active[0]

    def create(
        self,
        title: str,
        *,
        goal: str | None = None,
        acceptance: list[str] | None = None,
        complexity: WorkflowComplexity = WorkflowComplexity.NORMAL,
        task_provider: str | None = None,
        task_project_id: str | None = None,
    ) -> str:
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
            "task_provider": task_provider,
            "task_project_id": task_project_id,
            "agent_provider": "codex",
            "git": {"branch": None, "worktree": None},
        }
        self.write_json(path / "meta.json", meta)
        criteria = acceptance or ["Define acceptance criteria"]
        checked = "\n".join(f"- [ ] {item}" for item in criteria)
        self.write_text(
            path / "requirement.md",
            "# Requirement\n\n"
            f"## Goal\n\n{goal or title}\n\n"
            "## Background\n\n\n\n## Scope\n\n\n\n## Non-goals\n\n\n\n"
            f"## Acceptance Criteria\n\n{checked}\n",
        )
        self.write_text(
            path / "state.md",
            "# State\n\n## Phase\n\ndraft\n\n## Completed\n\nNone\n\n"
            "## In Progress\n\nNone\n\n## Pending\n\n- Define scope and plan\n\n"
            "## Blocked\n\nNone\n\n## Next Action\n\nDefine scope and acceptance criteria.\n",
        )
        self.write_text(path / "plan.md", "# Plan\n\n- [ ] Define scope and plan\n")
        self.write_text(path / "decisions.md", "# Decisions\n\nNo decisions recorded.\n")
        self.write_text(
            path / "verification.md",
            "# Verification\n\n## Unit Tests\n\nStatus: TODO\n\n"
            "## Type Check\n\nStatus: TODO\n\n## Integration Tests\n\nStatus: TODO\n",
        )
        self.write_text(
            path / "handoff.md",
            "# Handoff\n\n## Last Session\n\nNone\n\n## Completed\n\nNone\n\n"
            "## Files Changed\n\nNone\n\n## Current State\n\nWorkspace created.\n\n"
            "## Important Context\n\nNone\n\n## Next Recommended Action\n\n"
            "Define scope and acceptance criteria.\n\n## Known Problems\n\nNone\n",
        )
        self.write_json(path / "sessions.json", [])
        return requirement_id

    def load(self, requirement_id: str) -> dict[str, Any]:
        path = self.path_for(requirement_id)
        if not path.is_dir():
            raise WorkspaceError(f"Workspace not found: {requirement_id}")
        missing = [name for name in ("meta.json", *WORKSPACE_FILES, "sessions.json") if not (path / name).is_file()]
        if missing:
            raise WorkspaceError(f"Workspace {requirement_id} is incomplete: {', '.join(missing)}")
        return {
            "path": path,
            "meta": self.read_json(path / "meta.json"),
            "sessions": self.read_json(path / "sessions.json"),
            **{name.removesuffix(".md"): (path / name).read_text(encoding="utf-8") for name in WORKSPACE_FILES},
        }

    def touch_meta(self, requirement_id: str, **changes: object) -> dict[str, Any]:
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
            raise WorkspaceError(f"Cannot read {path}: {exc}") from exc

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        WorkspaceStore.write_text(path, json.dumps(value, ensure_ascii=False, indent=2))

    @staticmethod
    def write_text(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
        temporary.replace(path)
