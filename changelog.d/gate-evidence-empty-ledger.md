### Fixed
- **Issue #308：零 gate 設定下模型自述 gate_evidence 不再觸發 fail-closed**：
  operator 顯式未宣告任何 `PSC_GATE_CMD_*`（ledger `gates: []`）時，
  `authorize_terminal` 跳過 gate_evidence 的 unknown-gate 對照（自述視為
  vacuous，#261 文件本就聲明零 gate＝無 R2 保護）；ledger 非空時維持原
  fail-closed。修正 gpt-5.4 builder 隨機把 shell 指令填進 gate_evidence
  導致同卡重派時好時壞的問題。
