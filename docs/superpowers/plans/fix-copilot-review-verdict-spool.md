---
status: accepted
work_item: fix-copilot-review-verdict-spool
---

# fix-copilot-review-verdict-spool Todo

## Problem

The Cortex foreign-review lane passes Copilot a per-job verdict spool directory,
but the headless Copilot argv does not grant the narrowly-scoped permissions
needed to write that one JSON file or run the declared read-only checks. The
reviewer can inspect the candidate, yet the Manager receives `verdict-missing`
and must keep the slice at `needs_human`.

## Scope

- When `verdict_spool_dir` is present, add only Copilot permissions for the
  exact spool verdict file, `rg`, and `python3` checks.
- Do not add `--allow-all`, `--allow-all-tools`, `--allow-all-paths`, or shell
  redirection permissions; the candidate worktree remains outside the write
  grant.
- Preserve the existing argv byte-for-byte when no verdict spool is requested.

## Acceptance

- Unit tests assert the exact `write(<spool>/verdict.json)`, `shell(rg:*)`, and
  `shell(python3:*)` grants and reject broad bypass flags.
- Existing launcher and verdict-channel tests pass.
- A live Cortex Copilot `gpt-5.4` review writes the per-job verdict spool and
  Manager harvests it through `cortex complete`.
