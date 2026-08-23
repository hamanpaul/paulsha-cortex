---
status: accepted
work_item: trust-root-copilot-toolchain-pinning
---

# Trust-root Copilot toolchain pinning Todo

Issue: `hamanpaul/paulsha-cortex#681`.

## Boundary

Ensure a Copilot job resolves the exact installed toolchain binary and
version promised by the registry. Do not solve a wrong binary by broadening
`PATH`, searching the operator's HOME, or accepting a mutable shim.

## Tasks

- [ ] Reproduce the shim-versus-real-binary resolution under the job's
      sanitized environment and record the absolute path and version.
- [ ] Make the generated toolchain wrapper resolve to the pinned binary/tree,
      reject symlink/path escape, and fail closed on a missing or mismatched
      version; keep operator and job identities distinct.
- [ ] Add tests for PATH/HOME removal, system-version shadowing, symlink and
      traversal attempts, exact version metadata, and idempotent reinstall.
- [ ] Include the wrapper in generated-vs-installed attestation and prove the
      production Copilot invocation uses the pinned path.
- [ ] Run focused tests, full pytest, policy/preflight, independent review,
      delivery, CI, and close the issue through Cortex.
