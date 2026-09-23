---
status: accepted
work_item: legacy-checkpoint-contract
issue: TBD
domain_breadth: 0
state_consistency: 1
invariant_count: 5
artifact_classes:
  - documentation
---

# E — checkpoint contract 設計

## Decisions

- **D1** — Only an #862 owner-accepted extension of the existing writer is in scope. The contract preserves its absent-to-complete single transaction and complete receipt. Proof of filesystem state and proof of historical Work Item ownership remain distinct inputs, both exact-bound to the same target. Receipt-first replay avoids adding a prepared registry state.

## Dependency and failure boundary

Requires explicit #862 owner acceptance and the #968 A1/#969 marker contracts; it also consumes #971's WorkAuthority/action boundary. 缺少任何契約、owner、權限、lineage、proof 或 exact target 時，一律 fail closed；不得以同名 row、caller claim、fixture shortcut 或舊 marker補足。

E owns the canonical versioned J input payload schema in its sibling spec. J depends on that stable contract; I implements a caller that emits it and consumes J, so there is no J↔I dependency. The payload binds request ID, WorkAuthority, proposed identities, exact row fingerprints, OS-authenticated operator provenance, invocation-bound workspace proof, observed_at, and a digest over the complete frozen envelope. J reselects rows and revalidates immutable lineage inside the same transaction.

## Five-dimension sizing

依 repo fix-standard 与 current_sizing_snapshot helper 計分：domain_breadth=0、state_consistency=1、acceptance_surfaces=2（2 gate-spine 加 R-09/R-16/R-19）、spec_stability=0（本三件套不含 open-question blocker）、orchestration=2（9 cards/9 persona bindings），預期總分 5 / Yellow。Sibling todo 必須記錄實際 helper output；此分數不解除上游 hard dependency。

## Evidence and limit

#862 is OPEN and I09 describes absent-to-complete checkpoint creation with a complete receipt. This child is contract-only; owner acceptance is an external hard dependency.
