---
status: accepted
work_item: self-publication-receipt-append
issue: 993
domain_breadth: 0
state_consistency: 2
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# Child B — registry append implementation plan

## Authority and dependencies

- Child B of #978; live issue is #993. Accepted contract: self-publication-receipt-append-spec.md and self-publication-receipt-append-design.md.
- Sole production owner: paulsha_cortex/coordinator/registry.py. Child A workflow.py freezes typed values; Child C github_delivery.py owns read-only PR GET.
- Hard implementation prerequisite: #966 merged full registry raw-byte CAS for every _persist(); #862 slice CAS is not enough. Coordinate same-file ownership/API/merge order with #968 first. #967 Manager lock is not a substitute. #978 aggregate additionally retains its live #862 precondition.
- #979 needs A+B. #980 needs A+B+C plus #982/#983. #978/#963/#847 remain open until aggregate acceptance; AC10 stays at #965/#847.

## Five-dimensional sizing

feature-oneshot projection; recompute on actual work registration:

| Dimension | Score | Evidence |
|---|---:|---|
| domain_breadth | 0 | one production module: registry.py |
| state_consistency | 2 | full-file multiwriter CAS, same-row append/patch, rollback |
| acceptance_surfaces | 2 | gate spine 4 + R-09/R-16/R-19 |
| spec_stability | 0 | accepted trio |
| orchestration | 2 | 11 cards and 11 bindings |
| **Total** | **6 / Yellow** | combo projection only |

## Tasks

- [ ] T0 — prerequisites/owner gate: verify #966 merged raw-byte revision CAS, inspect exact API, coordinate with #968 owner; do not touch registry production code before both gates.
- [ ] T1 — carry-forward: preserve A typed/opaque/invalid-container state through registry load, manual WorkflowRun reconstruction, ordinary update and copy; preserve duplicate/non-finite row token in the exact JSON-safe diagnostic wrapper, and prove wrapper reload never promotes it.
- [ ] T2 — request boundary: prove create/start/intake/general update reject or ignore public receipt injection/replacement/removal while preserving pre-existing state.
- [ ] T3 — narrow internal append: require trusted producer path, expected whole-file revision, exact run/work/repo/claim; accept a non-empty typed receipt sequence from one producer kind/event ID plus one producer patch and append the full batch in one `_persist()`.
- [ ] T4 — collisions/replay: implement exact pair and object uniqueness rules across each batch; allow same event across distinct output publication IDs; exact whole-batch replay is a no-op; partial prior batch or any conflict writes nothing.
- [ ] T5 — CAS/batch/race tests: one multi-output brainstorm batch plus coupled fields produces exactly one persist; exact batch replay is a no-op; stale revision restores memory and returns explicit conflict; use two processes to show no lost update and no merge/retry.
- [ ] T6 — reachable negative: temporary repo, WorkAuthority-mapped pre-existing foreign file, formal work-action start and intake separately, production starter, exact post-reload planning_authority/hash, no valid receipt.
- [ ] T7 — failure matrix: invalid-container, ambiguous raw row, duplicate-key/non-finite wrapper tampering, cross-run/claim, wrong PR POST-vs-GET id, atomic persist/fsync/rename failure and reload all fail closed.
- [ ] T8 — docs/changelog/policy: document writer contract, add changelog fragment and Unreleased entry, run focused tests and PR-context policy preflight.
- [ ] T9 — evidence accounting: report test endings, CI/review/merge and installed/loaded separately; no AC10 claim.

## Stop conditions

If #966 is absent or incompatible, #968 ownership cannot be serialized, exact CAS cannot restore memory on conflict, or state cannot be atomically written, stop this implementation as blocked and preserve the issue boundary; do not downgrade state score or rely on #862/#967.
