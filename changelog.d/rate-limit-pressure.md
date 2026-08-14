---
type: fix
scope: monitor
---
**Issue #506（部分實作）：GitHub 掃描 burst 減壓——請求節流與 403 分診／退避**

Monitor 的 `_github_refresh_loop` 每輪對每個 GitHub repo 跑兩個 provider 的
`scan()`，而單 repo 單輪的 `gh` 呼叫是 O(issues 分頁 + remote todo 檔數 +
workflow-linked merged PR 數) 而非 O(1)；約 40 個 repo 的 workspace 一輪會在
數秒內齊發數百次請求，穩定觸發 GitHub secondary（abuse detection）rate limit，
`github:` 與 `github-terminal:` 兩個 provider 同時 degraded（實測超過 35 分鐘），
operator 的 `cortex work` 全被 `provider-authority-rate-limited-canonical` 擋下。

- **請求節流（攤平）**：新增 `monitor/github_pressure.py` 的 `GitHubPressureGate`，
  在**每一次** `gh` 請求前插入 `interval + jitter` 間隔（含 graphql 分頁與逐檔
  `contents`）。參數由 `monitor:` 區段的 `github_request_interval_ms`（預設 200，
  設 0 即停用）與 `github_request_jitter_ms`（預設 100）控制。預算計算：40 repo ×
  約 5 次呼叫 × 0.2s ≈ 40s ≪ 一輪 300s；另有 `github_throttle_budget_seconds`
  （預設 120，且夾在 refresh interval 的一半以下）作為極端情況的上限保護，
  預算用盡後節流自動失效，不讓節流本身把掃描週期撐爆。閘門由 `WorkModelRefresher`
  持有並跨 repo／跨輪次共用（壓力是 per-token 而非 per-repo）。
- **403 分診**：命中 rate-limit 型失敗時再查一次 `gh api rate_limit`（此端點不計
  配額）：`remaining > 0` → `github secondary rate limit`；`remaining == 0` →
  `github primary rate limit exhausted`；探測失敗則退回既有的
  `github rate limit exceeded`。三者都仍被 `is_rate_limit_signal` 認得，
  `coordinator/claim.py` 的 `provider-authority-rate-limited-canonical`
  reason code 行為不變（#370 的成果，已用測試鎖住）。
- **退避**：命中後開啟指數退避窗（`github_backoff_base_seconds` 預設 60、
  `github_backoff_max_seconds` 預設 1800），並尊重訊息中透出的 `Retry-After` /
  `x-ratelimit-reset`；退避期間 provider 的 `scan()` 直接跳過、**不發任何請求**，
  而不是每輪再硬撞一次 403。`GitHubTerminalProvider` 原本把限流混進
  `github terminal evidence unavailable`、下游完全分辨不出來，本次一併接上同一套
  分診與退避，並且不再重試 rate-limit 失敗。

未注入閘門時 provider 行為與先前完全相同（節流／退避皆停用）。GraphQL 批次化、
全域跨子系統預算、`gh auth status` 雙重驗證與緊迴圈輪詢規範不在本次範圍，
留給 #506 的後續票。
