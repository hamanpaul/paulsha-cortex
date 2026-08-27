---
status: accepted
work_item: phase2-rc-rollback-order
---

## Why

Exact-main RC qualification run `32981448179` failed during its real systemd-container rollback:
the installer intentionally retained a receipt-bound content-addressed venv and fresh checkout, but
the unknown-state scanner classified their newly-created carrier parents as unknown. The harness
also created non-transactional runtime scaffold fixtures before exercising rollback, mixing two
ownership domains inside the rollback proof.

## What Changes

- Classify a retained filesystem entry as managed only when its path or carrier relationship is
  proven by the archived receipt inventory.
- Continue reporting every foreign sibling and toolchain drift; do not weaken fail-closed rollback.
- Move harness-authored runtime scaffolding after rollback and clean reinstall.
- Re-run the exact feature SHA through the same RC container workflow before merge, then re-run the
  final-main RC before release.

## Capabilities

### Added Capabilities

- `trust-root-phase2-closeout`: explicit rollback classification contract for retained managed
  subtrees versus foreign siblings.

## Impact

- Changes only installer rollback classification, qualification ordering, focused tests, and their
  docs/spec/changelog. No production service or `/opt/cortex` mutation is performed locally.
