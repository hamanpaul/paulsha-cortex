# generated-asset-attestation

- [ ] Generate a complete inventory for every unit, shim, polkit rule,
  gitconfig, and toolchain wrapper, with exact content hashes and ownership.
- [ ] Compare generated inventory with the installed runtime and fail on
  functional drift while allowing comment-only warnings.
- [x] Add regression coverage for the three-way-to-four-way trust-root and
  missing GitHub credential surfaces without printing credential contents.
- [ ] Run focused/full gates, Docker qualification, exact-candidate review,
  delivery, required CI, merge, and issue closure through Cortex.
