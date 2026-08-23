---
status: accepted
work_item: manager-gitconfig-delivery
---

# Manager Git Credential Delivery Specification

## Requirements

- Generate the Manager HTTPS credential-helper configuration from the trust-root
  authority; never hand-edit or print credential contents.
- A dry-run credential lookup MUST prove the helper is reachable without changing
  a remote ref or exposing a token.
- `recover-repair-commit` MUST consume builder HEAD evidence through an
  authorized ledger/handoff channel, not by reading the builder tree as Manager.

## Acceptance

- Generated/installed config hashes, redacted lookup result, handoff evidence,
  focused/full gates, review, delivery, CI, and issue closure are all recorded.
