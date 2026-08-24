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
      authoritative pre-archive gate inputs on this descendant Candidate
      (`openspec validate <change> --type change --strict` plus policy check);
      preserve any sandbox-only backend dependency misses separately from
      Candidate-local failures.
- [x] Revalidate the exact Candidate against the independent HOME review
      findings and confirm the typed OpenSpec/policy gates on its descendant;
      keep the pinned source plan unchanged and make no archive, merge, or
      issue-closure claim in this active change.
- [x] Recheck the repair regressions: missing HOME remains a reachable
      fail-closed status, unresolved account ownership is rejected, the shim
      rejects missing directories, and shared test homes are owned by a
      `TemporaryDirectory`; keep the pinned source plan unchanged.
- [x] Repair the pre-archive ACL provisioning regression: access and default
      ACLs are applied in the permgen order so recursive provisioning remains
      portable, and HOME account resolution cannot bypass owner validation;
      keep the source plan pinned and leave Manager-owned archive/merge/issue
      closeout actions pending.
- [x] Remove the redundant generated main-spec shadow and lock both legacy and
      explicit change-scoped strict OpenSpec validation to the active change;
      keep archive, merge, issue closure, and done claims Manager-owned.
> Manager-owned closeout remains pending: official OpenSpec archive/commit,
> exact-PR-head delivery and CI evidence, merge, issue closure, and done.
