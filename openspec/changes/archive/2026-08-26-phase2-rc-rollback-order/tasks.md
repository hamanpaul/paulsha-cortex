---
status: accepted
work_item: phase2-rc-rollback-order
---

# Tasks

- [x] Reproduce the failed RC state with RED tests for a fresh carrier parent and early scaffold ordering.
- [x] Extend archived-receipt path classification without suppressing foreign siblings.
- [x] Move runtime scaffold creation after rollback and clean reinstall.
- [x] Pass focused backend/transaction/qualification tests and the complete pytest suite.
- [x] Synchronize canonical docs, changelog fragment, and strict-valid OpenSpec delta.

## Post-merge operational closeout

The feature SHA and final-main SHA must each pass the real RC container workflow. Only the final-main
run may unlock `v0.1.10`; its Actions and Release records are external evidence, not pre-completed
OpenSpec task checkboxes.
