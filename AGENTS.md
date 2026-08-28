# Project guidance

- Keep V1 local-first and Python 3.11+.
- Treat `V1架构.md` as the V1 scope and acceptance source.
- Default to the lightest workflow that can safely solve the task.
- Prioritize Restore, Checkpoint, Handoff, and Context Snapshot before external integrations.
- Keep Core independent of dashi-taskboard, Codex, Multica, Obsidian, and Git implementations.
- Preserve existing user files and human-readable Markdown/JSON state.
- Do not add a database, Web UI, multi-Agent scheduler, or automated knowledge writes in V1.
