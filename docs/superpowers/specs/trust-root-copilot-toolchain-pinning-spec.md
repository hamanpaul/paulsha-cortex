---
status: accepted
work_item: trust-root-copilot-toolchain-pinning
---

# Trust-root Copilot toolchain pinning Specification

## Requirements

- A Copilot job MUST execute the exact registered binary and version, resolved
  without broad PATH/HOME search or a mutable shim.
- Missing, symlinked, traversing, wrong-owner, or version-mismatched binaries
  MUST fail closed before a job starts.
- The wrapper, registry entry, and installed path MUST be covered by tests and
  generated/install attestation.

## Acceptance

- Tests cover sanitized PATH/HOME, shadowed system versions, symlink escape,
  exact metadata, reinstall, and rollback; runtime evidence identifies the
  binary path and version without secrets.
