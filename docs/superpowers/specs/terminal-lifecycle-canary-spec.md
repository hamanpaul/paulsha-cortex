---
status: accepted
work_item: terminal-lifecycle-canary
---

# Terminal Lifecycle Canary Specification

## Requirements

### Terminal lifecycle evidence

The lifecycle-terminal-live-canary capability SHALL leave reviewable terminal live-canary evidence in `docs/unified-work-lifecycle.md`.

The evidence SHALL cover confirmed Todo and issue auto-label behavior, heterogeneous brainstorming, docs-only build execution, review, and strict delivery closure.

### Planning completeness

The initial OpenSpec change SHALL omit an accepted superpowers plan so that the completeness gate is forced to execute a heterogeneous brainstorm before accepted planning artifacts are published.

Accepted planning artifacts SHALL be produced only after the primary planner integrates the heterogeneous brainstorm evidence.

### Terminal closure

The canary SHALL verify terminal closure through archive, preflight, current-HEAD maintainer review, merge-commit delivery, issue closure, and done projection.

A missing typed output or failed gate SHALL prevent completion and preserve a `needs_human` state.

### Scope

The change SHALL be limited to the repo-local authority mapping, the dedicated
`docs/superpowers/workstreams/terminal-lifecycle-canary/todo.md`,
`docs/unified-work-lifecycle.md`, the `terminal-lifecycle-canary` OpenSpec
change, and issue #31.

The change SHALL NOT modify runtime code, directly alter the Manager registry, or enable automatic labels for other repositories.
