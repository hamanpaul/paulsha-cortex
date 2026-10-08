---
status: accepted
work_item: ship-preflight-evidence
---

# ship 階段 preflight 失敗留下 evidence 規格

## Requirements

對應 [#1366](https://github.com/hamanpaul/paulsha-cortex/issues/1366)。ship 階段的 preflight 失敗時，現在只剩 `RuntimeError: ship preflight failed: <stage>` 一行，policy 與 ci-parity 的輸出全部遺失。

1. **R1 失敗時寫 evidence**：`paulsha_cortex/coordinator/work_actions.py` 的 `_ship_action` 在 `run_preflight(...)` 回傳 `passed=False` 時，必須先寫出一份 `cortex-pr-preflight/v1` evidence，再結束這一步。格式與 `work_bridge._preflight_result_evidence` 相同：`policy` 與 `ci_parity` 各自帶 argv、returncode、stdout／stderr 尾段，各最多 2000 字元。`stage` 標成 `ship`，`status` 標成 `needs_human`，`reason` 標成 `ship-preflight-failed`。
2. **R2 理由帶 evidence 路徑**：拋出的錯誤訊息要包含 evidence 的路徑，讓 run 的 needs_human 理由和 `cortex inspect status` 都看得到。訊息開頭維持 `ship preflight failed: <failed_stage>`，既有比對這段文字的程式與測試不受影響。
3. **R3 所有失敗階段都涵蓋**：`policy`、`ci-parity`、`tree-race` 三種 `failed_stage` 都要寫 evidence。`policy` 失敗時 `ci_parity` 為 `null`，與 pr-preflight 階段一致。
4. **R4 通過時不變**：preflight 通過時不寫這份 evidence，後續流程與現行完全相同。
5. **R5 測試**：新增 `tests/test_ship_preflight_evidence_1366.py`，先 RED 後 GREEN：
   - (a) ci-parity 失敗：evidence 檔存在、欄位齊全，錯誤訊息含路徑。
   - (b) policy 失敗：evidence 存在，`ci_parity` 為 `null`。
   - (c) tree-race：evidence 存在。
   - (d) preflight 通過：不產生 evidence。
   既有 `tests/test_ship_*.py` 的斷言保留。

## Boundary

- 只改 `paulsha_cortex/coordinator/work_actions.py`。重用 `work_bridge._preflight_result_evidence`；如果它的參數不合用，可以在 `work_bridge.py` 做純加法的抽取，不改它既有的輸出。
- 不改 `preflight.py` 的判定、不改 tick 的例外處理流程、不改 pr-preflight 階段。
- spec／design／todo 的文字是 pinned authority，只能勾選 checkbox。

## Evidence

2026-10-08 05:19Z，run `workflow-bb957bedb3a90f1fc999`（#1302，PR #1331，head `79516168`）的 needs_human 理由只有 `RuntimeError: ship preflight failed: ci-parity`；同一個 head 的 GitHub CI 12/12 通過。operator 本機重跑三次都先在環境差異上失敗，始終沒有看到真正失敗的測試。
