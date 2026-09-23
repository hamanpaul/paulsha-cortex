---
status: accepted
work_item: legacy-workspace-proof-helper
issue: TBD
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# F — workspace proof helper 設計

## Decisions

- **D1** — F is observation-only and requires exact path from a trusted caller. Use actual Git metadata for .git directories and files. Treat marker absence as observation, not a repair request. Work Item lineage is independently revalidated by J.

## Dependency and failure boundary

Blocked by E contract and consumes #968 A1 identity and #969 marker-writer contracts. G separately owns installed cross-UID execution/transport. 缺少任何契約、owner、權限、lineage、proof 或 exact target 時，一律 fail closed；不得以同名 row、caller claim、fixture shortcut 或舊 marker補足。

## Five-dimension sizing

依 repo fix-standard 与 current_sizing_snapshot helper 計分：domain_breadth=0、state_consistency=1、acceptance_surfaces=2（2 gate-spine 加 R-09/R-16/R-19）、spec_stability=0（本三件套不含 open-question blocker）、orchestration=2（9 cards/9 persona bindings），預期總分 5 / Yellow。Sibling todo 必須記錄實際 helper output；此分數不解除上游 hard dependency。

## Evidence and limit

The current marker lacks durable #968 identities. A same-UID helper test validates helper behavior only and cannot satisfy the separate-UID service boundary.
