---
status: accepted
work_item: trust-root-agent-loop-qualification
---

# Trust-root real agent-loop qualification Design

## Decisions

- Reuse the generated service/unit and invoke the real configured agent command
  through the same launch seam as production.
- Separate qualification artifacts from durable credentials and make every
  degraded, fallback, quota, or model-identity result explicit and failing.
