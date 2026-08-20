# 765-claim-era-advance

- **`#765`（部分）advance 的 terminal-job 選擇以 claim era 過濾——authority
  restart 後 run 不再卡死於前代 job 的綁定失配。** #373 的 authority restart
  （operator link／PR 建立等 authority 前進）重算 claim_key 並把 verify/review
  打回 pending，語意是「在新 era 下重驗」；但 resume 的 job 選擇無 era 條件，
  前代 terminal job 被撿起 → `_job_for_workflow_card` 的 claim_key 綁定必炸且
  每 tick 重炸（實機：`work link openspec` 觸發 restart 後 run 永久卡
  `workflow job binding mismatch`）。修法：選擇條件加
  `workflow_claim_key == run.claim_key`；era 不符的 job 成為前代稽核列，
  `dispatch_or_stop` 為新 era 重新派工。派工端 matching（instance 編號唯一性）
  刻意維持全 era。#765 其餘（openspec-propose 回寫 authority 的 intake 側）
  另行處理。
