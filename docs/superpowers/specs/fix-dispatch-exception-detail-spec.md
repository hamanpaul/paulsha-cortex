---
status: accepted
work_item: fix-dispatch-exception-detail
---

# fix-dispatch-exception-detail Specification

#177 前置：修正 #100——`autonomy.dispatch_ready()` 收集的 per-slice 例外只進入 `DispatchReadyError` 的 slice id 清單，底層例外（如 `FileNotFoundError` 路徑）不進 tick response 也不進 manager.log；且 manager.log 每行無時間戳，無法區分新舊錯誤。

## Requirements

### R1 DispatchReadyError 攜帶 per-slice 例外摘要

`paulsha_cortex/coordinator/autonomy.py::DispatchReadyError` 的可讀訊息 MUST 含 per-slice 底層例外摘要（slice id + `type(e).__name__: <message>`，每則 message cap 長度避免 flood）。既有 `jobs`（成功 jobs）欄位 MUST 保留；`errors`（dict slice→Exception）欄位 MUST 保留供透傳。

### R2 例外寫入 tick response

`manager_daemon` 的 tick handler 遇 `DispatchReadyError` 時 MUST 把 `errors` 轉成 list[dict]（含 slice id 與例外 type/message）寫入 tick response 的 `errors` 欄位，使 operator 不必離線重演即可定位。

### R3 manager.log ISO-8601 前綴

`manager_daemon` 的 log 輸出 MUST 每行前置 ISO-8601 時間戳（UTC 含毫秒）；行尾內容不變，保持既有 grep/parse 相容。

### R4 限制

- stdlib-only；TDD（`DispatchReadyError.__str__` 含摘要；tick response `errors` 透傳；log capture fixture 驗證首欄 ISO-8601）。
- 不改對外 CLI `--json` envelope schema 字串。
- `test_zero_dependency_runtime` 續綠；`policy_check --repo .` 0 fail。
- 不處理 #152/#99。