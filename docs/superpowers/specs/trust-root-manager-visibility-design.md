---
status: accepted
work_item: trust-root-manager-visibility
---

# Trust-root Manager Visibility Design

## Design

The implementation exposes generated trust-root service identity and desired
state through structured lifecycle and receipt channels.  It fails closed on
absent, mismatched, or unverifiable installed state and never infers
credentials from an operator home directory.
