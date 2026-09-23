---
status: accepted
work_item: automatic-main-sync-retry-stop
domain_breadth: 0
state_consistency: 2
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Automatic main-sync retry 與 durable stop（#973 Child B2）

## Boundary

- Scope: manager.py automatic caller and existing dispatch integration.
- Dependencies: #972 merged; A and B1a complete; B1b and B1c independently complete after B1a; B0 registry budget contract merged after hard prerequisite #966. B1 aggregate is tracking-only.
- #972 supplies tuple but no counter. B0 alone owns limit=1, used and derived remaining in WorkflowRun attempts; B0 requires #966 whole-registry revision CAS. #990 alone owns durable stop writing.
- B2 uses B1's exact-M transport/task and A's selector/provenance. B3 owns proof; C1/C2 own delivery stages.
- Same-file merge order: A → B1a → B2 → B3 → C1.

## Tasks

- [ ] T1 Trace resume/periodic entrypoints; only consider same-run typed main_sync context with Candidate CAS C and permitted classification.
- [ ] T2 Before reset, require A's producer resolution, B1a exact-M source pin, and B1c exact C/M task plus deterministic private-ref name.
- [ ] T3 Ask B0 to persist the immutable typed snapshot, consume the one-unit reservation and reset the final Builder card in one #966-protected CAS; conflict means no reservation/no dispatch.
- [ ] T4 After reset and before Builder launch, require B1b to explicit-fetch M into the independent Builder clone and verify private ref==M and HEAD/feature/base==C.
- [ ] T5 Dispatch/reconcile at most one current-era Builder job with deterministic reservation idempotency key and `dispatch_head=C`; on provisioning refusal use B0 snapshot with #990, without refund.
- [ ] T6 Make re-entry recompute the same reservation key and reuse/reconcile the bound job; no additional budget consumption or non-idempotent duplicate dispatch.
- [ ] T7 On exhausted/missing/corrupt context, M/C mismatch, refused reset, #966 CAS conflict or ambiguous dispatch relation, call #990's accepted writer with live context or B0 snapshot and valid existing recovery next_actions.
- [ ] T8 Assert every refusal has no Builder launch/preflight/push/PR/Copilot effect and never reads latest main as fallback.
- [ ] T9 Verify terminal/uncertain dispatch after reservation retains the consumed unit and stops for operator recovery; no budget refund/retry loop.

## Sizing

One production module gives domain_breadth=0; tuple/budget reservation/run reset/job identity/re-entry gives state_consistency=2. Accepted fix-standard mechanics score 6 / Yellow.
