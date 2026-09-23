---
status: accepted
work_item: legacy-caller-auth
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# H — operator authentication 設計

## Decisions

- **D1** — OS UID is a local process principal, not a human GitHub identity. A root-managed exact allow policy is required and defaults to deny. requested_by remains display/audit only. Keep this principal type narrowly scoped to I.

## Dependency and failure boundary

Blocked by E contract and #971's confirmed WorkAuthority/action source. Supplies a principal to I; ordinary control requests remain unchanged. 缺少任何契約、owner、權限、lineage、proof 或 exact target 時，一律 fail closed；不得以同名 row、caller claim、fixture shortcut 或舊 marker補足。

## Five-dimension sizing

依 repo fix-standard 与 current_sizing_snapshot helper 計分：domain_breadth=1、state_consistency=1、acceptance_surfaces=2（2 gate-spine 加 R-09/R-16/R-19）、spec_stability=0（本三件套不含 open-question blocker）、orchestration=2（9 cards/9 persona bindings），預期總分 6 / Yellow。Sibling todo 必須記錄實際 helper output；此分數不解除上游 hard dependency。

## Evidence and limit

The live control contract stores requested_by supplied by the client. It has no authenticated operator principal; this issue adds one only for migration.
