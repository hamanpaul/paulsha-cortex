# Design: Phase 2 RC rollback order

## Decision 1: archive inventory is the only retained-subtree authority

`rollback_journal` preserves the full set of filesystem steps after live journal entries are
removed. The scanner derives managed paths from asset, repository, toolchain, and venv steps in that
archive. A current inventory row is known only when it equals, contains, or is contained by one of
those receipt-bound paths. This permits necessary carrier directories while a nested retained venv
or checkout remains.

The scanner still recursively reports paths outside those relationships. Toolchain trees retain
their separate member-by-member manifest check, so adding them to the parent carrier exclusion does
not hide modified or foreign toolchain members.

## Decision 2: test installer rollback before runtime fixtures

The RC harness proves fresh apply, idempotent apply, drift rejection, rollback, and clean reinstall
before it creates the scaffold state needed by later runtime/service probes. This keeps the rollback
proof scoped to installer-owned state and prevents fixture ordering from deciding transaction
success.

## Rejected alternatives

- Treat every non-empty fresh parent as safe: rejects fail-closed foreign sibling detection.
- Teach rollback about qualification-only scaffold paths: creates a harness-specific exception in
  production authority.
- Retry the failed RC unchanged: the deterministic state classification would fail again.
