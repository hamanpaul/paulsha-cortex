---
status: accepted
work_item: generated-asset-attestation
---

# Generated Asset Attestation Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#695`.
- Scope is generated-versus-installed attestation for every registered
  root-owned Cortex artifact and the matching scheme-upgrade runbook.
- Do not silently rewrite installed assets during diagnosis; drift detection
  must report before an explicit installer action.

## Tasks

- [ ] Derive the complete attestation inventory from the canonical asset and
  generator registries, including units, shim, polkit, gitconfigs, and
  toolchain wrappers.
- [ ] Fail on functional non-comment drift, warn on comment-only drift, and
  expose exact artifact/reason evidence in `doctor` or Phase 2 selfcheck.
- [ ] Add deterministic generated-versus-installed tests plus negative
  controls for missing, malformed, stale-scheme, and comment-only artifacts.
- [ ] Update the scheme-upgrade runbook to reinstall the full derived
  inventory, then run focused/full gates, exact-HEAD review, delivery,
  required CI, merge, and issue closure through Cortex.
