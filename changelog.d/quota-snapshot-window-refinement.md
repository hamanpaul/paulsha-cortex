---
type: fixed
scope: coordinator
---
修正 #1099 同值 quota snapshot 的 window epoch refinement：當同時間的新 observation 補足
既有 unknown window epoch 時，file ledger 與 memory ledger 會追加 observation，shadow
projection 在同時間的 snapshot 間優先使用 epoch 已知者，使後續同窗口 terminal usage
可扣減。值不同仍記為 conflict，較不完整與完全相同的重送仍是 duplicate；沒有新增
ledger event kind 或 schema version。refinement 後再收到衝突值時，conflict receipt 的
`existing_sha256` 指向最近接受 observation 的 payload digest。
