---
status: accepted
work_item: copilot-review-late-observation
---

# Copilot 準時提交、晚觀測的 exact-HEAD review 規格（#1020）

## Requirements

本 work item 對應 [hamanpaul/paulsha-cortex#1020](https://github.com/hamanpaul/paulsha-cortex/issues/1020)。live issue 記錄：request `2026-09-23 17:42:59 UTC`、同 exact HEAD review submitted `17:45:41 UTC`（+162 秒）、Manager observed `17:58:43 UTC`（request 後 +944 秒）；review 應採信，但目前以觀測時間計 15 分鐘而回 `copilot-review-timeout`。

1. **R1 時限依提交時間**：非採信既有 review 的 request-bound 流程，review 只有在 `requested_at <= submitted_at <= observed_at` 且 `submitted_at - requested_at <= REVIEW_TIMEOUT_SECONDS` 時才算準時。時限上界維持含等號。`observed_at - requested_at` 超過時限本身不得否定已準時提交的 review。
2. **R2 真正逾時仍阻擋**：review 提交時間晚於 `requested_at + REVIEW_TIMEOUT_SECONDS` 時回 `needs_human / copilot-review-timeout`；期限屆滿仍沒有可用 review 時，既有「無回覆」逾時路徑也維持 `copilot-review-timeout`，不得因輪詢而延長期限。
3. **R3 時間與身分異常 fail closed**：`submitted_at < requested_at` 或 `submitted_at > observed_at` 不得採信；review 必須仍符合現行 exact HEAD、Copilot reviewer、有效 review state、non-error 及 review ID 綁定。舊 HEAD 不得被 current review 取代；error review 不得通過。
4. **R4 未解 current thread 仍阻擋**：current、未解且非 outdated 的 blocking review thread 仍不得進入 merge authorization；依現有路徑回到修復／review-thread gate。時間修正不能把它當作零 finding。
5. **R5 保留獨立 ship gates**：PR mergeable 狀態、exact-head CI terminal-green、review/thread、ForeignReview、issue/OpenSpec 及既有 merge-authorization gate 各自維持。此修正不得 merge、略過 CI、改寫授權或把本地證據當 remote gate 證據。
6. **R6 保留 #948 adoption 契約**：既有 review 的採信時間仍依 `adopted_at` 計算，原有 request 前 review adoption 行為不因本票變成一般 request-bound review；#948 的既有測試須保留。
7. **R7 回歸證據**：測試須固定重現 request 後 162 秒提交、944 秒觀測，並同時證明真正晚提交、無 review、舊 HEAD、error review、future/早於 request 時間、未解 current thread、dirty/non-green CI 都不能取得 merge authorization。

## Boundary

Production 預期只改 `paulsha_cortex/coordinator/delivery.py` 的 `ReviewLoop.record_review()` 時限判式。`paulsha_cortex/coordinator/work_actions.py` 已把 persisted request time、remote review submission time 與 Manager observation time 一併傳入 `ReviewLoop`；僅在整合回歸證明 caller 另有阻擋時才重新評估此邊界並重算 sizing。

不改 15 分鐘時限，不改 GitHub request、review 身分／state 判式、review thread 定義、`evaluate_delivery_gate()`、merge 實作、maintainer `review-attest`／#871 fallback 授權、#948 的 adoption 語意、durable journal/schema 或 CLI。PR #986 當時 DIRTY／0 checks 是 issue 的交付邊界證據，不能由本票放行。

## Evidence

基底 `e74750dd74065fe0c911069adf609a4238148d47`：`delivery.py:203-227` 驗證 submitted epoch，之後卻用 `now_epoch - requested_at` 判 15 分鐘；`work_actions.py:6097-6154` 先按 current HEAD/reviewer/request epoch 找 review，命中後將 submission 與 observation epoch 同時傳給 `ReviewLoop`。故 issue 的真實路徑在 `ReviewLoop` 被晚輪詢時間誤判。`github_delivery.py:128-172` 仍獨立檢查 remote HEAD、mergeability、terminal-green checks、exact current review、error state 及未解 thread。既有測試 `tests/test_delivery_orchestrator.py:264-277` 涵蓋真正晚提交；`tests/test_github_delivery.py:167-184` 涵蓋 final gate 阻擋；`tests/test_copilot_review_adopt_existing.py` 涵蓋 #948 adoption。
