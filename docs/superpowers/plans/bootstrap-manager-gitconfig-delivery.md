---
status: accepted
work_item: manager-gitconfig-delivery
---

# Manager Git Credential Delivery Planning Bootstrap

## Tasks

- Read `docs/superpowers/workstreams/manager-gitconfig-delivery/todo.md`, issue
  `hamanpaul/paulsha-cortex#763`, and the current implementation/tests before
  writing.
- Create exactly these accepted planning-authority artifacts:
  - `docs/superpowers/specs/manager-gitconfig-delivery-spec.md`
  - `docs/superpowers/specs/manager-gitconfig-delivery-design.md`
  - `docs/superpowers/plans/manager-gitconfig-delivery.md`
- Every artifact must have frontmatter `status: accepted` and
  `work_item: manager-gitconfig-delivery`.
- The spec must contain an exact `## Requirements` heading; the design an exact
  `## Decisions` heading; the plan an exact `## Tasks` heading.
- Cover both bounded issue requirements: generated Manager HTTPS credential
  helper wiring without secret exposure or remote mutation, and
  `recover-repair-commit` consuming builder HEAD via an authorized handoff
  instead of Manager reading the builder worktree.
- Include RED tests, generated/install equivalence, cross-UID negative tests,
  rollback, full preflight, exact-HEAD review, CI/merge, and issue closure.
- Make no source-code, OpenSpec, changelog, registry, or installed-runtime
  changes in this slice. Commit only the three planning documents.

