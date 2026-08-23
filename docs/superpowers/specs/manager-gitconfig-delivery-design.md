---
status: accepted
work_item: manager-gitconfig-delivery
---

# Manager Git Credential Delivery Design

## Decisions

- Render one root-owned Manager gitconfig from the registry and validate its
  helper path and mode before activation; values remain outside logs/receipts.
- Treat builder-to-Manager transfer as a signed/hash-bound evidence handoff,
  with no cross-UID filesystem read shortcut and no remote-ref mutation in tests.
