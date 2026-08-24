---
status: accepted
work_item: trust-root-home-fail-closed
---

# Tasks

- [x] Preserve the HOME fail-closed implementation: launch validation rejects
      unset, blank, relative, symlinked, wrong-owner, and owner-unverifiable
      HOME values; PATH+HOME dual failures stay explicit; and the shim rejects
      missing HOME directories before exec/log takeover.
- [x] Cover the contract across the job runner, shim, generated unit/runtime
      environment, child inheritance, model `$HOME` expansion, and secret-free
      diagnostics, including the unresolved-account and missing-directory
      regressions from this repair.
- [x] Run focused HOME regressions, full `python3 -m pytest -q`, and the
      authoritative delivery preflight on this descendant Candidate; preserve
      any sandbox-only backend dependency misses separately from Candidate-local
      failures.
> Manager-owned closeout remains pending: official OpenSpec archive/commit,
> exact-PR-head delivery and CI evidence, merge, issue closure, and done.
