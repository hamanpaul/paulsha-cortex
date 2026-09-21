# 862 recovery-registry-receipt

- `JobRegistry` 現在對 slice 的 additive `recovery_receipts`／`recovery_receipt_history`／
  `recovery_checkpoints`／`recovery_dispositions` 做 fail-closed 載入驗證與深拷貝保護：
  live `recovery_receipts` 仍會鎖定當前 slice 的 target/caller 綁定，而
  `recovery_receipt_history`／`recovery_checkpoints` 改為只驗 immutable payload 的
  自洽性，不再因 slice 後續演進而被拒載；receipt version 也嚴格限定 exact-int v1。
  receipts/checkpoints 會拒絕未知 version、request bytes／payload digest／target
  mismatch 與跨 recovery/checkpoint 的 registry-wide `request_id` collision；
  dispositions 則只接受 versioned exact-key rows，unversioned 或未知鍵 payload
  會 fail-closed。合法 legacy rows 仍不補寫缺欄位，既有 v1 migration 與 #501
  repair 行為維持不變。
