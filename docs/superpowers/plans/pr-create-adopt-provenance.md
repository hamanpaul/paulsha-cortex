---
status: accepted
work_item: pr-create-adopt-provenance
issue: 982
domain_breadth: 0
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---
# GitHub PR Create/Adopt Provenance Work Plan (#982)

## Boundary

Child #982 of #963/#847; owner/work item `pr-create-adopt-provenance`. Accepted planning artifacts:

- [spec](../specs/pr-create-adopt-provenance-spec.md)
- [design](../specs/pr-create-adopt-provenance-design.md)

Production changes are limited to `paulsha_cortex/coordinator/github_delivery.py` plus focused tests/docs. #982 depends on #992 and #994; finish/merge #994 first, then serialize #982. Do not wait for #978 aggregate merge or modify the current `work_bridge.py` caller.

## Invariants tracked (8)

- **I01 Structured classification:** API distinguishes confirmed POST create, pre-existing adoption, and ambiguity.
- **I02 Legacy compatibility:** existing public method remains integer-returning until #980 migration; legacy number is never provenance.
- **I03 Canonical create body:** first POST includes the exact #992 marker and #994 rendering.
- **I04 No retrofit:** existing PR cannot receive, lose, or switch intent markers through adoption/metadata sync.
- **I05 Exact create witness:** `created` requires successful POST plus exact repo/positive number/positive REST ID/nonempty node ID.
- **I06 Ambiguous timeout:** lost/timeout or incomplete response stays ambiguous even with matching marker observation.
- **I07 Shared boundaries:** #992 owns schema/IDs; #994 owns GET; #983/#980 own journal; #993 owns append.
- **I08 Parent scope:** full #847 trace remains; consumers and AC10 canary stay with #964/#965/#847.

## Tasks

- [ ] **T01 predecessor/order gate:** Verify #992 and #994 contract/merge state. Integrate #994 before #982; do not wait for #978 or parallelize same-module work.
- [ ] **T02 additive structured API:** Add `create_or_get_pull_request_with_outcome()` with closed `created | adopted | ambiguous` result and exact available facts. Preserve `create_or_get_pull_request()` integer behavior and existing caller until #980 migration.
- [ ] **T03 confirmed create witness:** Return `created` only for successful authenticated POST with exact repo, positive number, positive REST ID, and nonempty node ID. Return tuple unchanged for #980/#983; malformed/incomplete response is `ambiguous`.
- [ ] **T04 exact create marker:** Send the Manager-supplied #992/#994 canonical body with one marker in the initial POST. Do not recompute the shared ID/digest or add a second validator.
- [ ] **T05 adopted and metadata paths:** Keep every pre-existing match `adopted` and return before metadata sync/write. Do not PATCH a marker onto it. The structured `created` result also returns the exact POST witness before any metadata PATCH; #994 then validates the untouched POST result. Separately invoked metadata sync after #980's valid-receipt/provenance guard may preserve exact marker and unrelated fields, but must reject deletion/replacement/addition and cannot change outcome.
- [ ] **T06 timeout and diagnostic lookup:** On lost/timeout POST, return `ambiguous` even if lookup observes matching marker/body/head. Never retry into a replacement create and never treat GET as creator proof.
- [ ] **T07 regression and compatibility matrix:** Cover response tuple success, incomplete/malformed response, existing unmarked/foreign/matching-marker PR, drift/duplicates, timeout plus matching marker, metadata marker preservation, and legacy int wrapper/current caller. Fake responses only; no live GitHub mutation.
- [ ] **T08 scope, tests, and parent accounting:** Stay within `github_delivery.py` plus focused tests/docs; cover source, tests, and documentation; use repo policy `changelog.d/<slug>.md` and `CHANGELOG.md` gates, assess CLI-help synchronization, and run applicable test gates for the eventual product PR. Preserve parent AC01–AC10 verbatim and keep #980 producer/#964 consumers/#965 canary separate.

## Sizing projection and measured completeness

Production scope is one module, `github_delivery.py`, so `domain_breadth=0`. The API classifies remote create/adopt/ambiguous state but owns no durable journal/registry transaction, so `state_consistency=1`. Remaining dimensions and total are recomputed with the repository's `current_sizing_snapshot()` over these exact artifacts and the current `feature-oneshot` combo.

## Measured planning checks (2026-09-24)

- `current_sizing_snapshot(workspace_root="/tmp/982", combo_name="feature-oneshot", artifact_rows=[{"kind":"spec","ref":"docs/superpowers/specs/pr-create-adopt-provenance-spec.md"},{"kind":"design","ref":"docs/superpowers/specs/pr-create-adopt-provenance-design.md"},{"kind":"plan","ref":"docs/superpowers/plans/pr-create-adopt-provenance.md"}])` returned `(5, "yellow")`.
- Formal dimensions: `domain_breadth=0`, `state_consistency=1`, `acceptance_surfaces=2`, `spec_stability=0`, `orchestration=2`; total `5/Yellow`. The combo contributes gate spine 4, 11 cards, 11 persona bindings, and applicable rules R-09/R-16/R-19.
- `assess_planning_completeness()` accepted spec, design, and plan with no missing kinds or blocking markers. `plan_review_gate()` passed completeness, contract compatibility, and envelope; envelope reported the expected `envelope_unavailable` bypass.
- This is the current combo projection from the repository helper. No dimension was reduced; re-run after formal work registration and before dispatch.

## Delivery states

- Planning artifacts: accepted in this intake packet; not published or bound in the repo.
- Product implementation/tests: not started.
- Commit/push/PR/merge: not performed.
- Manager intent/receipt, install, and live canary: not performed; owned outside #982.
