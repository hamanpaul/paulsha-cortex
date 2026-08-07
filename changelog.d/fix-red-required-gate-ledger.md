### Fixed
- **Issue #307：gate ledger 一致性檢查消費 `test_policy=red-required`，解除與 tdd-red 卡的結構性互斥**：
  tdd-red 卡（`execution.test_policy=red-required`）的正確產出是「新增並 commit
  會失敗的 RED regression test」；宣告 `PSC_GATE_CMD_PYTEST` 時，這張卡的 pytest
  gate *理應* failed，先前一律被 `_assert_terminal_gate_consistency` 判為與
  terminal 自稱 `passed` 矛盾 → `needs_human`，任何宣告 `PSC_GATE_CMD_PYTEST`
  的環境中 red-required 卡結構性永不可能通過。`_assert_terminal_gate_consistency`
  現在會從 job 綁定的 `WorkflowRun.steps` 查出目前 card 的 `test_policy`，交給
  `terminal_contract.authorize_terminal` 做語意反轉：只精準命中 ledger 中名為
  `pytest` 的那一項，exit code 精確等於 `1`（pytest 的 `TESTS_FAILED`：測試被收
  集、確實執行，且至少一個失敗）才視為合格 RED 並反轉為 `passed`；exit code `0`
  （全綠，未產生 RED）反轉為 `failed`；其餘 exit code（`2`／`3`／`4`／`5`，對應
  collection error／interrupted、internal error、usage error、no tests
  collected）維持既有 `failed` 判定，避免「builder 根本沒寫測試」或「測試檔壞
  掉」被誤判為合格 RED。反轉只作用於矛盾偵測，不影響模型自述 `gate_evidence` 的
  誠實性 cross-check（仍對照未反轉的原始 ledger）；其他 gate（`openspec`／
  `policy`…）與一般卡（`test_policy` 非 `red-required`）完全不受影響，仍維持既
  有 fail-closed 行為。
