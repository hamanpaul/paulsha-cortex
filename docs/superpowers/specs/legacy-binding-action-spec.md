---
status: accepted
work_item: legacy-binding-action
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# I — explicit migration action 規格

## Authority and scope

Add the only production caller that can request explicit migration, route it through the existing Manager mutation seam, and orchestrate auth, exact history, proof, confirmation, CAS and replay.
Blocked by E, G, H, J and #971's WorkAuthority/action contract; requires #968 A1 identity APIs.

## Requirements

- **R1** — Add explicit cortex work migrate-legacy-slice action; it uses the existing Manager mutation seam and never runs implicitly from recover-pre-candidate/retry.
- **R2** — Require H-authenticated principal and confirmed WorkAuthority. Display and require exact operator confirmation of slice/job/WorkflowRun fingerprints and proposed identity.
- **R3** — Preflight and J same-CAS must both prove persisted slice→job→WorkflowRun lineage; immutable run repo/work_id exactly matches current WorkAuthority. Missing, ambiguous, foreign, malformed or drifted lineage returns needs_human and leaves legacy_unbound.
- **R4** — Only after old job/unit is stopped, request G proof for the exact workspace. Proof validates current repo/Git/path, not historical Work Item owner.
- **R5** — Serialize one request ID; look up complete receipt before any fresh proof. With no receipt, emit exactly E's versioned payload schema, freeze full proof/authorization/fingerprints/observed_at/digest and call J once. I consumes J; post-commit replay returns stored receipt without fresh timestamp/proof.
- **R6** — Positive tests use genuine retained historical lineage, not a synthetic legacy row paired with a newly written marker. Same-name foreign rows/workspaces are never selected or mutated.
- **R7** — If no real eligible legacy evidence or trusted principal/runner is available, mark blocked; do not claim #547 AC7 complete.

## Acceptance and verification

- [ ] Add explicit cortex work migrate-legacy-slice action; it uses the existing Manager mutation seam and never runs implicitly from recover-pre-candidate/retry.
- [ ] Require H-authenticated principal and confirmed WorkAuthority. Display and require exact operator confirmation of slice/job/WorkflowRun fingerprints and proposed identity.
- [ ] Preflight and J same-CAS must both prove persisted slice→job→WorkflowRun lineage; immutable run repo/work_id exactly matches current WorkAuthority. Missing, ambiguous, foreign, malformed or drifted lineage returns needs_human and leaves legacy_unbound.
- [ ] Only after old job/unit is stopped, request G proof for the exact workspace. Proof validates current repo/Git/path, not historical Work Item owner.
- [ ] Serialize one request ID; look up complete receipt before any fresh proof. With no receipt, emit exactly E's versioned payload schema, freeze full proof/authorization/fingerprints/observed_at/digest and call J once. I consumes J; post-commit replay returns stored receipt without fresh timestamp/proof.
- [ ] Positive tests use genuine retained historical lineage, not a synthetic legacy row paired with a newly written marker. Same-name foreign rows/workspaces are never selected or mutated.
- [ ] If no real eligible legacy evidence or trusted principal/runner is available, mark blocked; do not claim #547 AC7 complete.

## Non-goals

No registry writer, helper/runner implementation, auth policy implementation, shared recovery core, automatic resolver, or synthetic owner assignment.

## Verification boundary

此三件套 status=accepted 只代表 planning scope 已寫完整，不代表外部 issue owner acceptance、產品實作、測試、CI、merge 或 #547 closure。
