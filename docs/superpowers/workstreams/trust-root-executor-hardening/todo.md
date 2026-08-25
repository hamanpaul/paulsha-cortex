---
status: accepted
work_item: trust-root-executor-hardening
---

# Trust-root Executor Hardening Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#665`.
- Scope is limited to executor-side hardening for the trust-root install
  workflow; do not change unrelated provider or release policy.

## Tasks

- [ ] Enforce the declared UID/GID, path, symlink, ACL, and service-boundary
  checks before any privileged executor action.
- [ ] Add negative tests for path escape, account collision, unsafe ACL, and
  partial-apply/replay behavior.
- [ ] Run focused/full repository gates and record the candidate evidence in
  Cortex before delivery.
