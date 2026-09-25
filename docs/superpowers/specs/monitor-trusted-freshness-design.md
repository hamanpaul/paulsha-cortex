---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
---

# Monitor trusted freshness 設計

## Decisions

### D1 — Hash the exact parsed input

Correlation parser回傳與解析同一讀取的 `.cortex/work-items.yaml` bytes revision。使用 SHA-256；缺檔使用固定 absent sentinel，parse error不產生成功 manifest。Provider revisions與source map取自同一 generation消費的 candidate source snapshot。

### D2 — Snapshot read-back precedes success

candidate durable write後從 `WorkSnapshotStore` path reload；比較 canonical digest、sequence、目標 rows與 provider/source revisions。任一差異都將該代留在 running/failed，不得發布 succeeded。

### D3 — Freshness is a pure read oracle

唯一 API從 Monitor已配置的 canonical repo root載入 current input；載入最新 attempt marker及snapshot，驗證成功 generation、manifest與 `stale_after_seconds`。API回傳 immutable typed trust result和固定 failure reason，不寫檔、不修復狀態、不接受 caller path/hash。

### D4 — Consumer boundary

#1078只供給 Monitor freshness verdict與證據欄位。#1065接入 WorkAuthority，#1054比對 run/claim並執行 first-Builder gate；各自維持原 owner。

## Open Questions

- 無。
