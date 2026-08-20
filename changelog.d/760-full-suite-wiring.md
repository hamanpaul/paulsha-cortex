# 760-full-suite-wiring

- **`#760` `--skip-tests` 的 FullSuiteEvidence 契約接上 production——delivery 不再
  於 manager 環境（第三個 env-red 執行面，#723 第五例）第三跑全套而結構性卡死。**
  契約（tree-hash 定址、immutable、age 上限、`run_preflight` 內建 load＋驗證）
  早已完整，但 producer（`write_full_suite_evidence_after_run` 零 production
  caller，verification-28 F2 點名）與 consumer（work_bridge 兩個 call site 硬編
  `skip_tests=False`）都沒接。修法：(1) producer＝build 候選採信點（harvest 後），
  **未反轉**的權威 ledger pytest 實際 passed（tdd-red 的 RED 天然排除）時以
  `git rev-parse <candidate>^{tree}` 記 `record_external_full_suite_evidence`
  （自 `write_full_suite_evidence_after_run` 抽出的共用 writer、格式不變、冪等）；
  best-effort，記錄失敗不影響採信。(2) consumer＝`_run_exact_candidate_preflight`
  兩個 call site 以 `fresh_full_suite_evidence`（存在＋未過期）預判請求
  `--skip-tests`；缺席／過期照舊 False，最終驗證仍由既有 `--skip-tests` 契約
  單一把關。
