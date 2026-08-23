---
status: accepted
work_item: trust-root-manager-visibility
---

# Trust-root Manager Visibility Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#623`.
- Scope is limited to trust-root service visibility, identity, and lifecycle
  handoff; do not broaden it into provider credentials or release policy.

## Tasks

- [ ] Make Manager-visible units, identity, and lifecycle state derive from
      trust-root desired state and remain observable after install/restart.
- [ ] Add regression coverage for Manager discovery, service identity, and
      fail-closed behavior when installed state is missing or mismatched.
- [ ] Run focused/full gates and record candidate evidence through Cortex.
