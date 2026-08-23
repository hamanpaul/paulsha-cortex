---
status: accepted
work_item: trust-root-home-fail-closed
---

# Trust-root HOME fail-closed Design

## Decisions

- Validate HOME in the structured launch contract and export only the approved
  principal directory; never derive it from operator HOME.
- Use the same validator for plan, generated unit, shim, and pre-launch checks,
  with secret-free error codes for every rejection.
