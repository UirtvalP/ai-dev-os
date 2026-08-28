---
name: workspace-orchestrator
description: Resume and advance persistent AI development requirement workspaces across replaceable Codex sessions. Use when creating, continuing, checkpointing, handing off, reviewing, or checking status for a named requirement; do not use for one-off work with no persistent workspace.
---

# Workspace Orchestrator

Treat the Workspace as the source of truth and the current Thread as disposable.

1. Detect the current Requirement Workspace. If none exists and the user wants persistent work, create one through the project CLI.
2. Restore requirement, state, handoff, plan, decisions, verification, task-provider state, and Git state before relying on chat history.
3. Use the lightest safe workflow: tiny for obvious local changes, normal by default, complex for evidenced cross-module or high-risk work, and research when the answer requires investigation.
4. Execute and verify the current task. Do not create plans merely for process compliance.
5. Checkpoint after a meaningful stage. Generate a handoff when the current Thread can end.
6. Never mark a Requirement done without explicit user approval after acceptance and verification review.

Core project rules and V1 scope live in `V1架构.md`. The workspace system owns persistent state; this Skill only follows and updates it.
