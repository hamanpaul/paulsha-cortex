---
status: accepted
work_item: trust-root-home-fail-closed
---

# Tasks

- [x] Preserve the HOME fail-closed implementation: launch validation rejects
      unset, blank, relative, symlinked, and wrong-owner HOME values, PATH+HOME
      dual failures stay explicit, and the shim never falls back to daemon/unit
      HOME.
- [x] Cover the contract across the job runner, shim, generated unit/runtime
      environment, child inheritance, model `$HOME` expansion, and secret-free
      diagnostics with focused trust-root regressions.
- [x] Exercise the pre-archive local gates on this Candidate: focused HOME
      regressions and full `python3 -m pytest -q` pass here, canonical OpenSpec
      specs validation passes, and the offline delivery preflight only stops at
      this sandbox's missing installed `policy_check` module rather than a
      Candidate-local failure.
- [ ] Manager-owned closeout remains pending: official OpenSpec archive/commit,
      exact-PR-head delivery and CI evidence, merge, issue closure, and done.
