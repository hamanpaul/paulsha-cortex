---
status: accepted
work_item: trust-root-executor-hardening
---

# Trust-root Executor Hardening Design

## Decisions

Executor actions consume only the structured desired-state and receipt data
from the trust-root plan.  They reject path escape, symlink substitution,
account/ownership drift, unsafe ACL masks, and non-replayable partial state;
unknown durable state is preserved rather than deleted.
