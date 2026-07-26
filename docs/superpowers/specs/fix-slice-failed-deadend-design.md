---
status: accepted
work_item: fix-slice-failed-deadend
---

# fix-slice-failed-deadend Design

## Decisions

### D1 failed → needs_human

新增 `failed` 的 outgoing transition 至 `needs_human`。`needs_human` 已有 `retry-build`，形成完整恢復路徑：`failed → needs_human → (retry-build) → building`。安全：operator 先檢視再 retry。

### D2 registry 磁碟重載

daemon 在處理 action 前檢查 `jobs.json` mtime；若較記憶體載入時間新則重新讀取。或提供 `registry reload` 命令。採 mtime 比較為主動方式，不需 operator 手動 reload。

### 風險與 mitigation

- mtime 比較在快速連續修改時可能漏 → 加最小間隔或 atomic write（temp + rename）。
- `failed → needs_human` 語意需清晰 → transition action 命名為 `ack-failure` 或 `request-review`。