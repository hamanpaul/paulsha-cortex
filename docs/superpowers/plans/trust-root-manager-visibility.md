---
status: accepted
work_item: trust-root-manager-visibility
---

# Trust-root Manager Visibility Plan

Issue: `hamanpaul/paulsha-cortex#623`.

## Boundary

Make the generated trust-root service and runtime state visible to Manager
under the same identity and lifecycle contract used by installation.  Do not
infer credentials from the operator home or change unrelated release policy.

## Tasks

- [ ] Trace the generated unit, environment files, service identity, and
      Manager discovery path under the protected installation layout.
- [ ] Implement the smallest correction so Manager can reach and verify the
      declared repo/runtime state after install and restart.
- [ ] Add fail-closed tests for missing, mismatched, or unverifiable state and
      keep all evidence credential-free.
- [ ] Run focused tests, full pytest, policy/preflight, independent review,
      delivery, CI, and issue closure through Cortex.
