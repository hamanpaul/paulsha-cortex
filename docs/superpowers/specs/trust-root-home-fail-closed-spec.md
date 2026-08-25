---
status: accepted
work_item: trust-root-home-fail-closed
---

# Trust-root HOME fail-closed Specification

## Requirements

- An unset or empty HOME MUST be a launch configuration error, not a best-effort
  fallback.
- The approved per-principal HOME MUST be absolute, owned by the principal,
  non-symlinked, and validated before executor launch.
- Tests MUST cover missing/empty/wrong HOME, PATH+HOME together, inheritance,
  model `$HOME` use, and redaction of diagnostics.

## Acceptance

- Generated unit, wrapper, and runtime environment agree on the same HOME
  contract and remain idempotent across reinstall and rollback.
