# 750-repair-review-feedback

- **`#750` repair 回合終於看得到打回它的 verification 判定——盲修不收斂的迴圈
  關掉。** #606 的 retry_context 只看同一張卡的前次 job；verification 判定在另一
  張卡上、且因 harvest fail-closed（verify terminal 只認 verified/passed）沒有
  綁進 run，`retry-build` 的 repair 文案要求 builder 依「current verification/
  review evidence」修 candidate、卻沒有任何通道把 evidence 交到它手上（實機：
  verification-22 實質 failed → repair -23 只補了測試 → verification-24 同因再
  failed）。修法：`_prior_review_rejection(run, registry)` 經 harvest 同一支
  `_extract_terminal_json` 讀回本 run 最近一顆 verify／review 非通過 terminal 的
  summary／findings／conformance（各自有界），**誠實標注
  `source: "reviewer-terminal"`**（reviewer 產物、非 manager-independent），由
  dispatch 端只在 builder build 卡上併進 retry_context 的 `review_rejection` 鍵。
  首派 prompt 逐字不變（#606 要求 2 不動）、採信端零改動、判定只進 prompt 不進
  evidence。
