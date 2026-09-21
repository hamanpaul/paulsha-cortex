---
type: feat
scope: coordinator
---
#948 ship 段採信既有 exact-HEAD Copilot review：`_ship_action` 在呼叫 `request_copilot` 前先檢查 `remote.copilot_reviews`，存在 exact-HEAD、Copilot、COMMENTED／APPROVED 且非 error review 時直接採信（多筆取最新 `(submitted_at_epoch, review_id)`），同 tick 進入 review 判定而不重複 request；`ReviewLoop` 支援 `adopted_at` 基準，採信 review 不受請求前 epoch 或 15 分鐘 timeout 誤判；候選 HEAD 前進時重新評估不沿用舊 review；`copilot-*` stop 的 `next_actions` 補齊 `review-attest` 重入出口並提示指令形式。
