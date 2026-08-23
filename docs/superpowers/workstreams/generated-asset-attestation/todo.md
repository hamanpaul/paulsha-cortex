---
status: accepted
work_item: generated-asset-attestation
---

# Generated Asset Attestation Todo

## Boundary

- Issue: `hamanpaul/paulsha-cortex#695`.
- Scope is limited to proving generated-vs-installed equivalence for Manager
  and Monitor units, trust-root credential surfaces, and the related RWP/UID
  attestation gap.
- Do not change provider credentials, production deployment state, or unrelated
  job sandbox policy in this work item.

## Tasks

- [ ] Generate a complete inventory for every unit, shim, polkit rule,
  gitconfig, and toolchain wrapper, with exact content hashes and ownership.
- [ ] Compare generated inventory with the installed runtime and fail on
  functional drift while allowing comment-only warnings.
- [ ] Add regression coverage for the three-way-to-four-way trust-root and
  missing GitHub credential surfaces without printing credential contents.
- [ ] Run focused/full gates, Docker qualification, exact-candidate review,
  delivery, required CI, merge, and issue closure through Cortex.
