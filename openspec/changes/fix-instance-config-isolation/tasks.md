---
status: accepted
work_item: fix-instance-config-isolation
---

# Tasks

- [x] 1.1 [RED] Add reproducible regression coverage for legacy env adoption,
      exact-project single-repo monitoring, rollback, shared config-root
      diagnostics, and post-migration managed-path drift.
- [x] 1.2 Implement the accepted legacy env migration and atomic rollback path.
- [x] 1.3 Implement exact-project workspace semantics without sibling scans.
- [x] 1.4 Add single-instance shared config-root diagnostics.
- [ ] 1.5 Run the GREEN implementation and repository governance gates before
      archive (Manager-owned final preflight).

      Pre-archive status for this card: the implementation-focused tests and
      policy/OpenSpec validation passed. The authoritative full pytest gate is
      pending because this sandbox rejects AF_UNIX socket creation with
      `EPERM`; Manager must rerun it in an environment with that capability.
      Archive, merge, issue closure, and done remain Manager actions.
