---
status: accepted
work_item: terminal-lifecycle-canary
---

# Terminal Lifecycle Canary Plan

## Tasks

### 1. Document the terminal lifecycle canary

- [ ] Update `docs/unified-work-lifecycle.md` with issue #31 and the confirmed `terminal-lifecycle-canary` mapping.
- [ ] Record persona-domain separation and the heterogeneous brainstorm requirement.
- [ ] Record the docs-only build path, verification gates, and fail-closed `needs_human` behavior.
- [ ] Record terminal closure through archive, merge-commit delivery, issue closure, and done projection.

### 2. Validate the change

- [ ] Verify that the resulting diff remains within the authorized documentation and OpenSpec scope.
- [ ] Run OpenSpec validation successfully.
- [ ] Run the repository policy checks successfully.
- [ ] Run the full preflight successfully.
- [ ] Complete ForeignReview successfully.
- [ ] Obtain an adversarial maintainer review of the exact current HEAD.

### 3. Deliver and close

- [x] Archive the `terminal-lifecycle-canary` OpenSpec change through the Manager-owned official archive flow.
- [ ] Deliver the accepted change using a merge commit.
- [ ] Close issue #31 through the delivered merge.
- [ ] Verify that the completed work is projected as done.
- [ ] If any required output or gate fails, retain `needs_human` and do not claim completion.
