---
status: accepted
work_item: ship-preflight-evidence
---

# ship 階段 preflight 失敗留下 evidence 設計

## Decisions

### D1 重用 pr-preflight 的 evidence 格式

pr-preflight 階段已經有 `work_bridge._preflight_result_evidence`，它依 #759 的規則把失敗的逐字原因寫進 `cortex-pr-preflight/v1`。ship 階段沿用同一個 schema 與同一個 evidence 目錄（`evidence/pr-preflight`），只用 `stage: "ship"` 區分。這樣 operator 和工具只需要認一種格式。

### D2 先寫 evidence，再拋出錯誤

`_ship_action` 的失敗出口仍然是拋出 `RuntimeError`，tick 的處理方式不變。差別只在拋出之前先寫 evidence，並把路徑附在訊息尾端：`ship preflight failed: ci-parity (evidence: <path>)`。訊息開頭不變，所以既有以前綴比對的程式不受影響。

### D3 寫 evidence 失敗不能蓋掉原本的錯誤

如果寫 evidence 本身失敗（例如磁碟錯誤），仍然拋出原本的 `ship preflight failed: <stage>`，並在訊息裡註明 evidence 寫入失敗。不可以讓 evidence 的錯誤取代 preflight 的錯誤。

### D4 測試做法

用假的 runner 與假的 preflight 結果，直接呼叫 `_ship_action` 走到 preflight 那一步，涵蓋 spec R5 的四種情況。不需要真的跑 pytest 或 policy_check。

### D5 Sizing

只改一個 production 模組（`work_actions.py`），state 只多寫一份 evidence 檔，`domain_breadth: 0`、`state_consistency: 0`，屬於小票。
