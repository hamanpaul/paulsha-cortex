# work-registry-excludes-migration

- **`#508` work registry 舊列補齊 `excludes`**——`_validate_override_payload()` 要求每列鍵集合
  恰為 `{title, links, excludes}`，而 `excludes` 是後加欄位；本 repo 57 個 work item 中有 42 個
  寫於該欄位引入前，導致**任一舊列毒化整份檔案**，`cortex work link/unlink` 對任何 work_id
  一律 `work override row malformed`。本次為資料面 migration：42 列補上 `excludes: []`，恢復
  registry 寫入能力（讀取層相容與錯誤訊息改善另於 `#508` 處理）。
- **Phase 1 兩個 workstream 佈線**——`fix-rate-limit-classification` 併入 `#506`（secondary
  rate limit 防護：403 分診 primary/secondary、憑證失效判定雙重驗證、禁緊迴圈輪詢、label
  讀取批次化為 O(1)、全域 API 預算節流）；新增 `fix-read-repo-tier-fail-closed` workstream
  對應 `#492`（缺 `tier` 的 fail-late 改為 dispatch 前 fail-closed）。
