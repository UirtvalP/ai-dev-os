---
name: workspace-orchestrator
description: Resume and advance persistent AI development requirement workspaces across replaceable Codex sessions. Use when creating, continuing, checkpointing, handing off, reviewing, or checking status for a named requirement; do not use for one-off work with no persistent workspace.
---

# Workspace Orchestrator

Treat the Workspace as the source of truth and the current Thread as disposable.

## Restore

1. Extract an exact `REQ-<digits>` identifier from the user's request when one is present. Never
   derive or rewrite the identifier.
2. Otherwise run `workspace current`. Continue only when it resolves exactly one active
   Requirement; ask the user to choose if multiple active Requirements are reported.
3. Run `workspace resume REQ-ID` before relying on conversation history. Treat its Context
   Snapshot as the current source for requirement, state, handoff, plan, decisions, verification,
   Dashi tasks, Git context, and next action.
4. If no Workspace exists and the user is starting durable work, create one with `workspace new`.
   Do not create persistent tracking for trivial one-off requests.

## Execute

- Follow the selected workflow in the Snapshot: tiny for an explicit local change, normal by
  default, complex for evidenced cross-module or high-risk work, and research for investigation.
- Prefer direct execution for tiny work. Do not create plans merely for process compliance.
- Preserve Requirement scope. Record a changed request instead of silently rewriting its history.
- Execute the next action, verify it, and keep external tools behind their configured Adapters.

## Persist

After a meaningful stage, persist the facts that changed:

```text
workspace checkpoint REQ-ID --phase PHASE \
  --completed "COMPLETED ITEM" \
  --next-action "NEXT ACTION" \
  --verification "Status: PASS - COMMAND OR EVIDENCE"
```

When the current Thread can end, generate the replaceable-session boundary:

```text
workspace handoff REQ-ID \
  --completed "COMPLETED ITEM" \
  --current-state "CURRENT STATE" \
  --important-context "IMPORTANT CONTEXT" \
  --next-action "NEXT ACTION"
```

Run `workspace review REQ-ID` only after acceptance criteria, verification, and configured Task
state are review-ready. Never mark a Requirement or Dashi Issue `done` without explicit user
approval.

Core project rules and V1 scope live in `V1架构.md`. The workspace system owns persistent state; this Skill only follows and updates it.
