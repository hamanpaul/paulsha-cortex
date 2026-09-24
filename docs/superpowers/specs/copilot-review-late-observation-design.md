---
status: accepted
work_item: copilot-review-late-observation
---

# Copilot 準時提交、晚觀測設計（#1020）

## Decisions

### D1 用 submission epoch 判斷 request-bound review 的期限

`ReviewLoop.record_review()` 保留 finite epoch、exact `head == self.head`、positive `review_id`、submitted-before-request 與 future-submission 驗證。當 `adopted_at is None`（一般 request 路徑）時，逾時量改為 `submitted_at - requested_at`；只有該差值大於 `REVIEW_TIMEOUT_SECONDS` 才回 `copilot-review-timeout`。因現有比較採 `>`，恰好 900 秒維持可接受。Manager observation 仍用於拒絕未來時間，但不再當作提交期限的時鐘。成功 decision 帶入經該函式驗證的 submission epoch，供下游 admission 使用；此為記憶體內證據，不新增 durable state。

例如 request=1000、submission=1162、observation=1944：提交延遲 162 秒，應繼續檢查 error/finding 與後續 gate，不能 timeout。request=1000、submission=1901（+901 秒）即使觀測時間也是 1944，仍 timeout。

### D2 保留既有兩種 timeout 情境

無 review 可用時，`work_actions._ship_action()` 現行 `now - requested_at > 900` 仍代表「期限已過仍未收到 review」，行為不變。已採信的 pre-request review（`adopted_at is not None`）繼續以 `now - adopted_at` 判定，保留 #948 語意。不要為了讓晚觀測通過而延長 request deadline、重新 request、寫入新時間戳或新增 durable state。

### D3 對齊最終 admission 的時限

`ShipOrchestrator.merge_if_ready()` 現行對非 adoption review 再以 `now - requested_at` 判 900 秒，會讓 `ReviewLoop` 修正失效。對 request-bound review 改用成功 decision 上經驗證的 submission epoch，重新檢查它是有限數、在 `requested_at` 之後且不晚於當前 observation，並以 `submitted_at - requested_at <= 900` 判期限；缺失、篡改或過晚一律 fail closed。對 #948 adopted review 保留 `now - adopted_at` 判式與既有測試。其餘 exact HEAD、review ID、fix budget、preflight、ForeignReview 與 final gate 條件照舊。

### D4 不變更 remote final gate

`work_actions._ship_action()` 仍先以 current HEAD、Copilot reviewer 與 `submitted_at >= requested_at` 篩 review，再由 `ReviewLoop` 作時限判定。review 通過後仍執行 `evaluate_delivery_gate()`；不更動 `github_delivery.py`。未解 current blocking thread 仍會被計為 finding，並由 final gate 再次阻擋；dirty PR、head race、缺失或非 terminal-green checks、error/無效 state、缺少 closing issue、OpenSpec/archive 或 ForeignReview 問題維持原阻擋。

### D5 狹窄回歸面

新增測試覆蓋 `ReviewLoop` 的提交／觀測分離與 900 秒邊界，以 orchestrator fake GitHub fixture 驗證晚觀測的成功 review 可走到 final admission，並以既有 `_ship_action` fake-GitHub harness 走真實 caller 路徑確認 #1020 時序。原有「請求已久即拒絕」測試須改成「提交真正逾時才拒絕」，另補缺失／非有限／future submission evidence 的負例。沿用既有 final-gate matrix 和 #948 adoption tests 做 regression evidence。若整合測試揭露 caller 必須改 production code，先擴大 production module mapping、domain/state 宣告及 sizing，再實作。

### D6 Sizing projection

預期 production module 僅 `delivery.py`，所以 `domain_breadth=0`；不增加或改寫 durable state，所以 `state_consistency=0`。以 repo `current_sizing_snapshot()` 對完整 accepted 三件組、`fix-standard` combo 計算 acceptance_surfaces、spec_stability、orchestration；見 Todo 的實際 helper 結果。若 production scope 或 triad 完整度改變，重新計分；若 Red，保留完整 AC 並提 issue-backed split，不得靜默縮 scope。
