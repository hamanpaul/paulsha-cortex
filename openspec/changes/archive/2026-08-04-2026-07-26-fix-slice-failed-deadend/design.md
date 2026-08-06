---
status: accepted
work_item: fix-slice-failed-deadend
---

# fix-slice-failed-deadend Design

## Decisions

### D1 failed → needs_human transition

新增 `failed → needs_human` transition，使 failed slice 可由 operator 手動介入後恢復。`needs_human` 已有 `retry-build`，形成 `failed → needs_human → (retry-build) → building` 恢復路徑。或者直接 `failed → building`（更激進但更直接）。

### D2 選擇 needs_human 而非直接 building

`failed → needs_human` 較安全——operator 先檢視 failed 原因再決定 retry，避免盲目重試。`needs_human` 的 `retry-build` action 已存在，重用即可。

### D3 registry 磁碟重載

registry daemon 定期（或在 action 處理前）檢查 `jobs.json` mtime，若較記憶體新則重載。或提供 explicit reload 命令。

### 風險與 mitigation

- `failed → needs_human` 可能讓 operator 誤以為需要人工 → action 名稱應清晰（如 `ack-failure` transition）。
- 磁碟重載可能有 race condition → 重載時加 lock 或以 atomic read 處理。