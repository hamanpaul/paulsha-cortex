---
status: accepted
work_item: terminal-lifecycle-canary
---

# Terminal Lifecycle Canary Design

## Decisions

### Repo-local authority

Use a repo-local override that maps issue #31, the dedicated Todo, and a new active, confirmed OpenSpec work item named `terminal-lifecycle-canary`. Do not modify the Manager registry directly or extend automatic labeling to other repositories.

### Isolated canary identity

Use a new issue, work ID, and OpenSpec change so that the canary remains isolated from previous lifecycle runs and their test isolation, resume-routing, active-run source-churn, and disposable-planner activation concerns.

### Planning authority

The initial change intentionally has no accepted superpowers plan. The completeness gate must run a heterogeneous `agy`/Google brainstorm, after which the primary planner integrates the evidence and publishes the accepted specification, design, and plan.

### Verification sequence

The canary verifies automatic claim, heterogeneous brainstorming, docs-only build, ForeignReview, archive, preflight, exact-current-HEAD maintainer adversarial review, merge-commit delivery, and done projection.

The maintainer review must target the exact HEAD being delivered. All other strict lifecycle gates remain unchanged.

### Fail-closed completion

Every required output and gate is authoritative. If any typed output is absent or any gate fails, the work remains `needs_human` and must not be projected as complete.

### Change boundary

The canary artifact change is documentation-only and is confined to the repo-local authority mapping, the dedicated Todo, `docs/unified-work-lifecycle.md`, the current OpenSpec change, and issue #31. Runtime delivery recovery may only re-arm a pre-binding target-cardinality stop after the authority becomes the exact single PR/OpenSpec/Todo tuple; all other strict gates remain unchanged.
