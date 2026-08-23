---
status: accepted
work_item: manager-gitconfig-delivery
---

# Manager Git Credential Delivery Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#763`.
- Scope is limited to generated Manager Git credential wiring and the adjacent
  `recover-repair-commit` cross-UID handoff gap described by that issue.
- Do not change Phase 2 job sandbox, prompt, attestation, or signing policy in
  this work item.

## Tasks

- [ ] Generate the Manager `credential.https://github.com.helper` entry from
  the trust-root authority; do not patch the installed `.gitconfig` by hand.
- [ ] Prove a Manager HTTPS push credential lookup through the generated
  root-owned config without exposing credentials or mutating a remote ref.
- [ ] Make `recover-repair-commit` consume builder-owned HEAD evidence through
  an authorized ledger/handoff channel instead of reading the builder tree as
  Manager.
- [ ] Run focused and full repository gates, generator/install equivalence,
  exact-HEAD review, delivery, required CI, merge, and issue closure through
  Cortex.

