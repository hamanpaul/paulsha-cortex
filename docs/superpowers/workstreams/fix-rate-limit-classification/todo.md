---
status: accepted
work_item: fix-rate-limit-classification
---

# fix-rate-limit-classification Todo

## Tasks

- [x] providers.py 分類順序修正（rate limit 判定先於 auth 字樣）
- [x] `_authority_from_canonical_row` 對 rate-limit degraded 給專屬 reason code
- [x] doctor gh-auth 區分限流與憑證失效
- [x] durable backoff deadline，operator resume 不再立即重撞

## Tasks（`#506`：secondary rate limit 防護，2026-08-13 fleet 升級實測事故）

`#370` 解決的是「限流被誤判成憑證失效」的**分類**問題；`#506` 是同一條路徑上的**用量**問題：
monitor 每 5 分鐘對全 fleet 掃描、auto-claim scan 每 tick 對每個 mapped issue 各發一次
`gh api` 讀 label（`coordinator/work_actions.py:3425`，per-tick O(n)），與 auto-advance／
foreign review／digest 共用同一個帳號配額。實測（2026-08-13 fleet 1.0.15→1.0.17 批次升級）
中 provider 反覆 degraded，`github:hamanpaul/paulsha-cortex` 長時間停在
`github rate limit exceeded`，導致 claim 全面 fail-closed——**primary 配額其實還剩 4800+**，
是 burst 觸發的 secondary limit。

- [ ] 403 分診：收到 rate-limit 型 403 時先查 `rate_limit` 端點（不計配額）分辨 primary／secondary；`remaining > 0` 即 secondary → 指數退避（尊重 `Retry-After`／`x-ratelimit-reset`），**不得**觸發任何憑證類 recovery
- [ ] 憑證失效判定雙重驗證：`gh auth status` 回報 invalid 時必須與 `rate_limit` 端點交叉確認才可下結論（限流期間 `gh auth status` 實測會誤報 token invalid）
- [ ] 禁止緊迴圈輪詢：CI／label／PR 狀態監看一律稀疏單次輪詢（≥3 分鐘＋jitter），不使用 `--watch` 類阻塞輪詢
- [ ] label 讀取批次化：scan 改以單一 GraphQL query 批量讀全部 mapped issues 的 labels（或 REST conditional request 吃 ETag／304），per-tick API 呼叫數自 O(n) 降為 O(1)
- [ ] 全域 API 預算節流：scan／auto-advance／foreign review／digest 共用一個 in-process rate budget，逼近上限時降頻而非硬撞
- [ ] 測試涵蓋：secondary 403（remaining>0）不得走憑證路徑、primary 403（remaining=0）睡到 reset、`gh auth status` 誤報 invalid 時的交叉驗證分支

