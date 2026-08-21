---
status: accepted
work_item: generated-asset-attestation
---

# Generated Asset Attestation Planning Bootstrap

## Tasks

- Read `docs/superpowers/workstreams/generated-asset-attestation/todo.md`, issue
  `hamanpaul/paulsha-cortex#695`, and the current generator/selfcheck/runbook
  implementation before writing.
- Create exactly these accepted planning-authority artifacts:
  - `docs/superpowers/specs/generated-asset-attestation-spec.md`
  - `docs/superpowers/specs/generated-asset-attestation-design.md`
  - `docs/superpowers/plans/generated-asset-attestation.md`
- Every artifact must have frontmatter `status: accepted` and
  `work_item: generated-asset-attestation`.
- The spec must contain an exact `## Requirements` heading; the design an exact
  `## Decisions` heading; the plan an exact `## Tasks` heading.
- Derive the attestation inventory from canonical registries, fail on
  functional non-comment drift, warn on comment-only drift, preserve a
  diagnosis-only mode, and cover all root-owned units/shim/polkit/gitconfigs/
  toolchain wrappers.
- Include missing/malformed/stale-scheme/comment-only negative tests, complete
  scheme-upgrade reinstall steps, rollback, full preflight, exact-HEAD review,
  CI/merge, and issue closure.
- Make no source-code, OpenSpec, changelog, registry, or installed-runtime
  changes in this slice. Commit only the three planning documents.

