---
status: accepted
work_item: trust-root-manager-visibility
---

# Trust-root Manager Visibility Specification

## Requirements

Given a planned or installed trust-root deployment, Manager can verify service
identity, generated unit inventory, receipt hash, and lifecycle state from
structured evidence.  Missing or divergent values block activation and are
reported without exposing credentials.

Regression tests cover fresh install, restart, drift, and missing-receipt
negative paths.
