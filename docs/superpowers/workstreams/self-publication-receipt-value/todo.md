---
status: accepted
work_item: self-publication-receipt-value
issue: 992
domain_breadth: 0
state_consistency: 1
invariant_count: 9
artifact_classes:
  - source
  - tests
  - documentation
---

# Child A — receipt value implementation plan

## Authority and boundaries

- Proposed child of #978/#963/#847; issue #992 已建立. Accepted contract: [spec](../../specs/self-publication-receipt-value-spec.md) and [design](../../specs/self-publication-receipt-value-design.md).
- Sole production module: paulsha_cortex/coordinator/workflow.py. Child A owns immutable receipt types and WorkflowRun parsing/serialization only.
- Child B owns registry.py manual reconstruction, ordinary update carry-forward, append API, whole-file CAS and rollback. Child C owns authenticated GET/read-back in github_delivery.py. #979 consumes A+B; #980 consumes A+B+C plus #982/#983. #978 remains open as aggregate.
- No repo, tests, GitHub or runtime was changed/run while preparing these accepted planning files.

## Five-dimensional sizing

Projection using the current feature-oneshot combo and repo helper; recompute after issue/work registration:

| Dimension | Score | Evidence |
|---|---:|---|
| domain_breadth | 0 | one production module: workflow.py |
| state_consistency | 1 | persistent WorkflowRun field and legacy serialization/reload boundary; no cross-object transaction |
| acceptance_surfaces | 2 | gate spine 4 plus applicable R-09/R-16/R-19 |
| spec_stability | 0 | accepted spec/design/todo |
| orchestration | 2 | combo has 11 cards, 11 persona bindings |
| **Total** | **5 / Yellow** | comparison projection, not registered claim-time score |

## Tasks

- [ ] T0 — golden canonicalizer: implement strict canonical JSON and domain-separated hash helpers matching accepted formulas; include fixed known-answer vectors.
- [ ] T1 — typed envelope: add frozen receipt and producer-specific accepted-input/evidence/object/transaction variants with exact keys/types; recompute receipt/event/publication/intent digests; reject cross-bindings.
- [ ] T2 — retain durable facts: retain full brainstorm report/default-pack/accepted-pack and plan ordered selection/assessment facts in immutable intent sidecars; retain PR intent reference and successful POST witness in receipt evidence. Validate all hashes and refs.
- [ ] T3 — tolerant WorkflowRun parser: missing key -> legacy empty; parse list entries independently; preserve unknown/malformed raw row plus diagnostic; preserve non-list invalid-container.
- [ ] T4 — round-trip and defensive copy: ensure to_dict/from_dict and WorkflowRun copies retain typed/opaque rows and invalid container exactly; nested raw mutation cannot alter stored values.
- [ ] T5 — collisions and identities: test same event/different publication allowed; exact pair replay stable; conflicting payload, duplicate publication across events, same output ref across different pair, invalid receipt ID, source_revision/commit-SHA/artifact-SHA type confusion rejected.
- [ ] T6 — PR trust limit: tests prove a marker/GET result without durable successful POST witness cannot parse as accepted Manager-created receipt; timeout/lost response remains ambiguous.
- [ ] T7 — docs/changelog/policy: update code docs, add changelog.d/self-publication-receipt-value.md and [Unreleased] entry, run focused tests and PR-context policy gates required by repo policy.
- [ ] T8 — evidence accounting: report focused/full tests and exact ending lines; distinguish implementation, tests, CI, merge, installed/loaded and live. Do not claim B/C, #979/#980, #964, #965 or #847 AC10 complete.

## Dependencies and remaining gates

A has no registry CAS prerequisite because it does not modify registry.py. Before implementation, freeze the common unions with #979/#980/#982/Child C and serialize the separate owners. Child B depends on this accepted schema; #979 depends on A+B; #982 depends on A/C marker contract; #980 depends on A/B/C/#982/#983. AC10 canary remains independently required by #965/#847 after the full repair is installed and loaded.
