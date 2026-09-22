---
status: accepted
work_item: copilot-review-adopt-existing
---

# Ship 段採信既有 exact-HEAD Copilot review 設計

## Decisions

### D1 採信是 request 的替代，不是 gate 的放寬

`_ship_action` 進入「需要 request」分支（`not ship or previous_head != preflight.head or phase ∉ {review-requested, merge-authorized}`）時，先從 `remote.copilot_reviews` 篩 exact-HEAD、Copilot、COMMENTED／APPROVED、非 error 的 review；命中則寫 ship 狀態 `{"phase":"review-requested","head","tree_hash","requested_at_epoch": review.submitted_at_epoch,"adopted_review_id": review.review_id,"adopted_at_epoch": now, ...}` 並**直接落入同一 tick 的 review 判定**（不回 `awaiting-copilot`）；未命中則呼叫 `request_copilot` 並維持現行狀態形狀（不寫 `adopted_*`）。`evaluate_delivery_gate` 的 `submitted_at_epoch >= copilot_requested_at_epoch` 因 `requested_at_epoch` 取自 review 本身而自然成立，判式與 policy 欄位不改。

### D2 `ReviewLoop` 的時間基準

`ReviewLoop` 新增可選 `adopted_at: float | None`；`record_review` 對採信 review 的 outside-request-epoch 判定改為 `submitted_at < requested_at`（仍成立，因相等）與 `submitted_at > now`；timeout 判定的 `elapsed` 以 `now - (adopted_at if adopted_at is not None else requested_at)` 計。非採信路徑 `adopted_at is None`，行為與現行逐字相同。

### D3 HEAD 前進即重評

採信欄位與 `head` 同生同滅：`previous_head != preflight.head` 已是現行「重新 request」的觸發條件，採信分支沿同一條件，舊 head 的 `adopted_review_id` 隨 ship 狀態整份覆寫；不另做跨 head 的 review 沿用。

### D4 stop 出口

`_phase_recovery_actions`（或 `_ship_action` 落 `copilot-review-timeout` 處）對 `copilot-*` stop 補 `review-attest` 進 `next_actions`，順序「既有值在前、補充在後」；`next_step_hint` 補 `cortex work review-attest <work_id> --repo <repo> --actor <operator> --payload <file>` 形式。不改 `abandon` 與 `retry-build` 的受理條件。

### D5 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| 採信 0 finding | fake GitHub adapter：`copilot_reviews` 含 exact-HEAD COMMENTED、`request_copilot` 計數 | `request_copilot` 0 次；同 tick `merge-authorized`；journal／evidence 帶 `adopted_review_id` |
| 採信有 finding | 同上 1 finding、thread open | `fix-required`（既有）；`review-thread-open` gate 不放寬 |
| error review | `is_error=True` | 不採信、`request_copilot` 1 次 |
| 舊 head review | `commit_id != head` | 不採信、request 1 次 |
| 40 分鐘後採信 | `now - submitted_at = 2400` | 不 timeout、不 outside-request-epoch |
| 無既有 review | 空 | 與現行測試逐字相同 |
| stop 出口 | 既有 timeout fixture | `next_actions ⊇ {abandon, review-attest}` |

### D6 Sizing

2 個 production 模組（`work_actions.py`、`delivery.py`；`github_delivery.py` 只加法投影）→ `domain_breadth=1`；ship 狀態新增兩個欄位、與 head 同生同滅、單 process 內判定 → `state_consistency=1`；三件齊全時機械三維固定 4，總分 6／Yellow。
