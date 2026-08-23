---
status: accepted
work_item: trust-root-executor-hardening
---

# Trust-root Executor Hardening Specification

## Requirement

Before privileged execution, the installer validates the desired-state hash,
filesystem containment, account identity, ownership/mode, ACL policy, and
transaction/replay receipt.  A failed check produces no partial destructive
mutation and blocks activation.

## Verification

Regression tests cover malicious paths, symlinks, UID/GID collisions, ACL
masks, interruption/replay, and idempotent application.
