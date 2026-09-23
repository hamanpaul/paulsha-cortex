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

# K — markerless recovery adapter 規格

## Authority and scope

Allow only explicitly migrated rows with complete record/receipt and fresh Builder proof to enter the existing shared recovery core; preserve all unbound rows as excluded.
Requires J, G, #968/#969 contracts, and explicit #970 owner acceptance for a separate shared-core adapter scope.

## Requirements

- **R1** — Accept only a J-created complete migration record/receipt with exact durable slice/repo/Work Item/attempt identities and validated historical lineage.
- **R2** — Obtain fresh nonce-bound read-only G/F proof for the exact stored workspace and compare repo origin, physical root/Git metadata, path, record digest and marker state.
- **R3** — Treat pre-#969 marker absence as explicit; never synthesize or write a new marker during recovery.
- **R4** — Both Manager and work entrypoints continue to use #970 shared admission/state/reclaim/handoff core; K adds only the legacy proof adapter and result mapping.
- **R5** — Missing record/receipt/proof, changed path/origin, identity drift, duplicate target or foreign same-name row fails before any workspace/registry/history/manifest effect.
- **R6** — Use a genuine retained markerless historical workspace and immutable lineage for the positive test; synthetic lineage, same-UID proof or new #969 marker is not evidence.
- **R7** — Rows lacking immutable lineage remain legacy_unbound/needs_human, even if Git origin or a new marker appears correct.

## Acceptance and verification

- [ ] Accept only a J-created complete migration record/receipt with exact durable slice/repo/Work Item/attempt identities and validated historical lineage.
- [ ] Obtain fresh nonce-bound read-only G/F proof for the exact stored workspace and compare repo origin, physical root/Git metadata, path, record digest and marker state.
- [ ] Treat pre-#969 marker absence as explicit; never synthesize or write a new marker during recovery.
- [ ] Both Manager and work entrypoints continue to use #970 shared admission/state/reclaim/handoff core; K adds only the legacy proof adapter and result mapping.
- [ ] Missing record/receipt/proof, changed path/origin, identity drift, duplicate target or foreign same-name row fails before any workspace/registry/history/manifest effect.
- [ ] Use a genuine retained markerless historical workspace and immutable lineage for the positive test; synthetic lineage, same-UID proof or new #969 marker is not evidence.
- [ ] Rows lacking immutable lineage remain legacy_unbound/needs_human, even if Git origin or a new marker appears correct.

## Non-goals

No migration action, identity writer, auth policy, checkpoint implementation, proof helper/runner, duplicated recovery state machine, terminal replay fix or retire-delivered cleanup.

## Verification boundary

此三件套 status=accepted 只代表 planning scope 已寫完整，不代表外部 issue owner acceptance、產品實作、測試、CI、merge 或 #547 closure。
