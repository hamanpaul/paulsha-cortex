---
status: accepted
work_item: trust-root-executor-hardening
---

# Trust-root Executor Hardening Plan

Issue: `hamanpaul/paulsha-cortex#665`.

## Boundary

Harden the trust-root executor around structured desired state, filesystem
containment, account identity, ACLs, and transaction replay.  Do not broaden
the change into provider credentials or release policy.

## Tasks

- [ ] Trace every privileged executor action and validate its declared UID,
      GID, owner/mode, ACL, path, and symlink assumptions before mutation.
- [ ] Reject path escape, account collision, unsafe ACL masks, and unknown
      durable state without deleting existing state.
- [ ] Add regression tests for negative paths, interruption/replay, and
      idempotent application, with redacted evidence.
- [ ] Run focused tests, full pytest, policy/preflight, independent review,
      delivery, CI, and issue closure through Cortex.
