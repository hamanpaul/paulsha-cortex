---
status: accepted
work_item: porcelain-inspect
---

# porcelain-inspect Design

## Goals

把六個分散的唯讀查詢方式（`status`/`jobs`/`stat`/`list`/`work show`/`doctor`）收斂成 `cortex inspect` 單一入口，並補上既有命令面缺乏的服務運行時真相（版本/exec path drift），不重新實作任何底層查詢邏輯。

## Decisions

- **薄包裝層**：`inspect <sub>` 內部直接呼叫既有 `status`/`jobs`/`stat`/`list`/`work show`/`doctor` 的既有函式，只做輸出格式轉換（human/`--json`），不重寫查詢邏輯，避免兩份實作漂移。
- **共用運行時探測模組**：新增 `paulsha_cortex/porcelain/_runtime_probe.py`，提供 `probe_service_runtime(instance)`（回傳模式、unit 狀態、pid、exec path、版本、`stale: bool`）；`inspect service` 為首個使用者，B4 `service status` 後續直接 import 復用，避免重複實作偵測邏輯。
- **殭屍偵測邏輯**：解析 systemd unit 的 `ExecStart` 取得 venv python 路徑，以 `Path.exists()` 判空；配合 `importlib.metadata` 版本與 unit 檔 mtime 推斷「跑著但來源已消失」。
- **human/json 一致性測試策略**：同一 fixture 資料同時斷言 human 輸出字串含關鍵欄位、`--json` 可 `json.loads` 且欄位語意相同，防止兩條輸出路徑各自漂移。
