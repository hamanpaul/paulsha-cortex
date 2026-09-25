---
status: accepted
work_item: delivery-journal-conditional-commit
issue: 983
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Delivery Journal Conditional Commit Work Plan (#983)

## Boundary

This is #983 under #963/#847 and a prerequisite for #980's durable PR intent/result. Accepted [spec](../specs/delivery-journal-conditional-commit-spec.md) and [design](../specs/delivery-journal-conditional-commit-design.md) are the normative planning packet. Product scope is one production module, `paulsha_cortex/coordinator/work_actions.py`, plus focused tests and documentation. Existing `work_bridge.py` consumers must use the protected `_save_runs` boundary without a production edit in this child. #978, #982, #979, and #980 retain their own contracts.

`domain_breadth: 0` reflects one production module. `state_consistency: 2` reflects cross-process serialization, revision/CAS, atomic replacement and fsync, and crash/replay durability for the shared journal. The remaining dimensions are computed from the accepted packet and loaded combo. Any second production module or new cross-store transaction requires scope and sizing review.

## Invariants tracked (8)

- **I01 Single boundary:** every Manager journal write, including existing bridge callers, passes through one protected writer.
- **I02 Exact baseline:** existence, monotonic revision, and exact content digest are compared while locked.
- **I03 No lost row:** a stale full-file snapshot never removes another committed run row.
- **I04 Immutable event:** intent precedes result; ordinary saves cannot add, remove, or change event entries.
- **I05 Exact replay:** same identity/kind/payload returns one committed event; changed payload conflicts.
- **I06 Durable confirmation:** committed follows file fsync, replace, directory fsync, and exact read-back.
- **I07 Unknown remains unknown:** persist-then-raise and crash windows cannot be reported as committed by the failed call; old callers receive an exception.
- **I08 Legacy and failure:** old v1 rows remain readable; malformed current data and lock/I/O failure fail closed.

## Tasks

- [ ] **T01 call-site and representation audit:** Inventory all `_load_runs`/`_save_runs` production calls in `work_actions.py` and `work_bridge.py`, including repeated saves on one mutable state and the fact that all 23 production save calls ignore return values. Check direct journal writes and test assumptions. Fix only the `work_actions.py` primitive; stop for scope review if a bridge edit is required.
- [ ] **T02 stable cross-process lock and baseline:** Add safe permanent lock-file handling, bounded acquisition, and a load token binding existence/revision/content digest. Keep legacy revisionless v1 readable; reject raw or stale snapshots on save. Ensure token refresh after a confirmed save supports a second save on the same state.
- [ ] **T03 conditional full-file commit:** Re-read current state under lock, compare the exact baseline, reject direct publication event additions, removals, replacements, and malformed/result-only entries, then write temp, fsync, replace, directory fsync, and confirm. Keep `_save_runs` success return compatible and raise on conflict/unknown/invalid so ignored returns cannot reach push or merge. The narrow append API exposes committed/conflict/unknown.
- [ ] **T04 opaque intent/result append:** Add a narrow run-keyed event operation with immutable identity/kind/payload hash, intent-before-result, exact replay, and conflict rules. Keep PR-specific fields, receipt validation, and registry writes with #978/#980.
- [ ] **T05 race and fault tests:** Use two independent processes or writer instances to prove stale different-run and same-run writes conflict without losing confirmed rows. Exercise explicit fresh recomputation, exact replay, changed canonical event payload, ordinary-save direct addition and result-only forgery, removal attempts, legacy load, malformed data, and interruption before/after replace and sync. Inject persist-then-raise; prove a legacy caller that ignores the return cannot continue to push/merge, and a fresh locked reader resolves exact event state without minting proof.
- [ ] **T06 source, tests, documentation, and delivery accounting:** Update developer documentation for baseline, conflict, and unknown outcomes. Check whether CLI help needs synchronization; this child adds no CLI surface unless implementation changes it. Run focused journal/ship tests plus repo-required tests and PR-context policy check. Add committed `changelog.d/<slug>.md` fragment and `CHANGELOG.md [Unreleased]` entry for product implementation. Review exact source diff, report skipped/deferred gates, and distinguish implementation, merge, installed runtime, and #847 canary evidence.

## Delivery states

- Planning packet: accepted spec/design/plan and a work item binding for #983 are published with this intake change; this is planning authority only.
- Product code/tests, commit, push, PR, merge, installed runtime, and live canary: not performed by this packet.
- #980 may consume the primitive only after #983's accepted implementation is merged. #963/#847 aggregate and AC06/AC10 gates remain open.
