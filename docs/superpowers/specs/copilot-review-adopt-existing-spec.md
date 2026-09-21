---
status: accepted
work_item: copilot-review-adopt-existing
---

# Ship 段採信既有 exact-HEAD Copilot review 規格

## Requirements

對應 [#948](https://github.com/hamanpaul/paulsha-cortex/issues/948)：GitHub 在 PR 建立當下即以 Copilot 對同一 HEAD 完成 review，Manager 晚於它才 `request_copilot` 時，既有 review 因早於 `requested_at_epoch` 被視為不存在，15 分鐘後必 `copilot-review-timeout`；Copilot 不對同一 HEAD 自動再審。

1. **R1 request 前先採信既有 review**：`_ship_action` 在 `github.request_copilot` 之前讀取 `remote.copilot_reviews`；存在 `commit_id == preflight.head`、`author == COPILOT_REVIEWER_LOGIN`、`state ∈ {COMMENTED, APPROVED}`、非 error 的 review（多筆取 `(submitted_at_epoch, review_id)` 最大者）→ 不呼叫 `request_copilot`、不開新 15 分鐘窗口，直接以該 review 進入既有 `ReviewLoop.record_review` 判定（0 finding → passed／merge-authorized；有 finding → 既有 `fix-required` 路徑；thread 未 resolve → 既有 `review-thread-open` gate）。無既有 review → 行為與現行完全相同。
2. **R2 採信的 review 不受 request 窗口限制**：採信路徑下 `ReviewLoop`／`evaluate_delivery_gate` 對該 review 的「`submitted_at >= requested_at`」與「`elapsed <= REVIEW_TIMEOUT_SECONDS`」判定必須成立：ship 狀態記 `phase: "review-requested"`、`requested_at_epoch = review.submitted_at_epoch`、新增 `adopted_review_id` 與 `adopted_at_epoch = now`；timeout 與 outside-request-epoch 判定對採信 review 以 `adopted_at_epoch` 為基準（review 早於採信時刻不算 outside、不算 timeout）。`copilot_review_id`／`copilot_requested_at_epoch` policy 欄位與 `evaluate_delivery_gate` 判式不改。
3. **R3 HEAD 換了不沿用**：候選 head 前進（`previous_head != preflight.head`）時重新評估：新 head 有既有 review → 再採信；沒有 → 走 `request_copilot`。舊 head 的 `adopted_review_id` 不得帶到新 head。
4. **R4 stop 出口補齊**：`copilot-review-timeout` 落 needs_human 時 `next_actions` 至少含 `review-attest`（maintainer 路徑，`_recoverable_maintainer_ship_stop` 已認 `copilot-*`）；`next_step_hint` 提示 `review-attest` 用法。不放寬 `abandon` 以外既有動作的受理條件。
5. **R5 證據**：採信時 `logger.info` 一行（run_id／head／review_id／submitted_at）並在 delivery journal 記 `adopted_review_id`；delivery evidence（`evidence/delivery-adapter`）帶同一欄位。
6. **R6 測試**：`tests/test_copilot_review_adopt_existing.py` RED→GREEN：(a) PR 建立即有 Copilot COMMENTED review（0 finding）、Manager 40 分鐘後才進 ship → 不 request、merge-authorized；(b) 同情境 1 finding → `fix-required`（既有路徑）；(c) 既有 review 為 error → 不採信、走 request；(d) 既有 review 是舊 head → 不採信；(e) 無既有 review → 呼叫 `request_copilot` 一次（現行）；(f) `copilot-review-timeout` 的 `next_actions` 含 `review-attest`。既有 `tests/test_delivery*.py`、`tests/test_github_delivery*.py`、`tests/test_work_actions_ship*.py` 原斷言保留。

## Boundary

Production 改 `paulsha_cortex/coordinator/work_actions.py`（`_ship_action` 採信分支、`_phase_recovery_actions`／next_actions）與 `paulsha_cortex/coordinator/delivery.py`（`ReviewLoop` 採信基準）；`github_delivery.py` 只在需要投影 `adopted_*` 欄位時加法修改、`evaluate_delivery_gate` 判式不改。不做 GitHub re-request fresh assessment（#948 建議 2）、不改 15 分鐘 timeout 值、不改 maintainer-review 路徑、不改 #943 CHANGELOG／#810 todo-incomplete 行為。

## Evidence

2026-09-22 00:04 CST pin `442fe23f`：#819 run `workflow-e75b36c500fb15fde025` PR #947 建立時 Copilot 即 COMMENTED（1 finding）；#847 restart 後 Manager 23:48 才 `request_copilot`，journal `phase=review-requested, requested_at_epoch=1790005696`，15 分鐘後 `copilot-review-timeout`，`next_actions` 只給 `abandon`；operator 以 `review-attest`→`resume` 場外出口。現行 `work_actions._ship_action` 只接受 `submitted_at_epoch >= requested_at`；`delivery.ReviewLoop.record_review` 對 `submitted_at < requested_at` 回 `copilot-review-outside-request-epoch`、`elapsed > REVIEW_TIMEOUT_SECONDS` 回 `copilot-review-timeout`。
