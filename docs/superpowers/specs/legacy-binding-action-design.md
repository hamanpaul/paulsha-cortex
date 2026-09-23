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

# I — explicit migration action 設計

## Decisions

- **D1** — One explicit CLI/work-action route calls the Manager mutation seam. User-supplied names are for confirmation only; exact persisted lineage and J CAS determine eligibility. Auth, proof and transaction are separate owner interfaces. I emits E's frozen versioned payload schema and consumes J; no eligible history means unbound.

## Dependency and failure boundary

Blocked by E, G, H, J and #971's WorkAuthority/action contract; requires #968 A1 identity APIs. J depends on E's payload contract, not I; I emits that schema and consumes J. 缺少任何契約、owner、權限、lineage、proof 或 exact target 時，一律 fail closed；不得以同名 row、caller claim、fixture shortcut 或舊 marker補足。

## Five-dimension sizing

依 repo fix-standard 与 current_sizing_snapshot helper 計分：domain_breadth=1、state_consistency=1、acceptance_surfaces=2（2 gate-spine 加 R-09/R-16/R-19）、spec_stability=0（本三件套不含 open-question blocker）、orchestration=2（9 cards/9 persona bindings），預期總分 6 / Yellow。Sibling todo 必須記錄實際 helper output；此分數不解除上游 hard dependency。

## Evidence and limit

No current production caller invokes an owner-authorized legacy migration API. This issue supplies that caller; it does not claim one already exists.
