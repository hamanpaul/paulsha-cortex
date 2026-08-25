---
status: accepted
work_item: trust-root-home-fail-closed
---

# Trust-root HOME fail-closed Todo

Issue: `hamanpaul/paulsha-cortex#692`.

## Boundary

Make the job environment contract symmetric: a missing `HOME` is a launch
configuration error, not a best-effort condition. Keep the existing PATH
fail-closed behavior and do not copy credentials from the operator account.

## Tasks

- [ ] Reproduce a job with zero extra environment and capture the exact
      pre-launch diagnostic for missing HOME.
- [ ] Validate and export an approved per-principal HOME before executor
      launch; reject unset, empty, relative, symlinked, or wrong-owner paths.
- [ ] Add tests for missing/empty/wrong HOME, PATH+HOME together, child
      environment inheritance, model `$HOME` failures, and secret redaction.
- [ ] Verify the generated unit, shim, and runtime environment all use the
      same HOME contract and remain idempotent across reinstall/rollback.
- [ ] Run focused tests, full pytest, policy/preflight, independent review,
      delivery, CI, and close the issue through Cortex.
