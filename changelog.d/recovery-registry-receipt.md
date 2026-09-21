# 862 recovery-registry-receipt

- `JobRegistry` 現在對 slice 的 additive `recovery_receipts`／`recovery_receipt_history`／
  `recovery_checkpoints`／`recovery_dispositions` 做 fail-closed 載入驗證與深拷貝保護：
  未知 version、request bytes／payload digest／target mismatch，以及跨 recovery/checkpoint
  的 registry-wide `request_id` collision 都會拒絕載入；合法 legacy rows 仍不補寫缺欄位，
  既有 v1 migration 與 #501 repair 行為維持不變。
