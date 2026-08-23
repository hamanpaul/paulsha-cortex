# trust-root-manager-visibility

## Task 1: Trace protected install discovery and service inventory

- [x] Add a RED regression reproducing generated trust-root manager/monitor units
      that still fail Manager bootstrap discovery when they point at the
      protected deploy EnvironmentFile.

## Task 2: Implement Manager-visible protected install state

- [x] Teach service discovery to accept the generated trust-root unit inventory
      and protected EnvironmentFile path while preserving repo/runtime identity
      checks.
- [x] Keep lifecycle visibility observable after install and restart without
      inferring paths from the operator HOME.

## Task 3: Fail-closed coverage and delivery

- [x] Add fail-closed coverage for missing, mismatched, or unverifiable
      installed state without leaking credentials.
- [ ] Run focused/full gates, policy/preflight, independent review, delivery,
      CI, and issue closure through Cortex.
