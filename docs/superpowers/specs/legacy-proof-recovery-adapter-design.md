---
status: accepted
work_item: legacy-proof-recovery-adapter
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# K — markerless recovery adapter 設計

## Decisions

- **D1** — K is a consumer adapter, not a migration path. Binding-time proof is historical evidence; recovery needs a fresh proof. Only J complete records become eligible. #970 owner must accept this new adapter scope rather than infer it from the current issue.

## Dependency and failure boundary

Requires J, G, #968/#969 contracts, and explicit #970 owner acceptance for a separate shared-core adapter scope. 缺少任何契約、owner、權限、lineage、proof 或 exact target 時，一律 fail closed；不得以同名 row、caller claim、fixture shortcut 或舊 marker補足。

## Five-dimension sizing

依 repo fix-standard 与 current_sizing_snapshot helper 計分：domain_breadth=1、state_consistency=1、acceptance_surfaces=2（2 gate-spine 加 R-09/R-16/R-19）、spec_stability=0（本三件套不含 open-question blocker）、orchestration=2（9 cards/9 persona bindings），預期總分 6 / Yellow。Sibling todo 必須記錄實際 helper output；此分數不解除上游 hard dependency。

## Evidence and limit

Live #970 owns shared transition behavior but does not currently claim migration-record proof adapter support. This child needs an explicit scope link/owner acceptance.
