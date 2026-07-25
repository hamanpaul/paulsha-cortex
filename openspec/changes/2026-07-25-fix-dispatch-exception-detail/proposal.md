---
status: accepted
work_item: fix-dispatch-exception-detail
---

## Goals

讓 dispatch 失敗時底層例外細節可被 operator 直接取得，並讓 manager.log 每行具時間戳以區分新舊錯誤，免除離線重演 debug 的高成本。

## Why

dogfood 實機驗收（#177）發現：`DispatchReadyError` 訊息只含 slice id 清單，底層例外（如 `FileNotFoundError` 路徑）不進 tick response 也不進 manager.log；且 manager.log 無時間戳，排查被舊 flood 誤導。operator 須離線重演 pin/dispatch 流程 debug，成本極高。

## What Changes

- `coordinator/autonomy.py`：`DispatchReadyError` 訊息含 per-slice 例外摘要（cap 長度）；`jobs` 保留。
- `coordinator/manager_daemon.py`：tick handler 把 `DispatchReadyError.errors` 寫入 response `errors`；log 每行加 ISO-8601 前綴。

## Capabilities

### Modified Capabilities

- `coordinator-dispatch`: 派工失敗診斷契約——per-slice 例外透傳與 log 時間戳。