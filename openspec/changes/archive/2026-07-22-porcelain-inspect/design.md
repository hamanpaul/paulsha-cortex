---
status: accepted
work_item: porcelain-inspect
---

# Design

## Decisions

- **薄包裝層**：`inspect` 六子命令內部呼叫既有 `status`/`jobs`/`stat`/`list`/`work show`/`doctor` 函式，不重寫查詢邏輯。
- **共用探測模組先行**：`_runtime_probe.py` 由本批次建立，B4（porcelain-service）的 `service status` 後續直接復用，避免殭屍偵測邏輯兩處各自實作。
- **一致性優先**：human 與 `--json` 輸出必須來自同一組內部資料結構轉換而成，禁止兩條獨立格式化路徑。
- **查無對象 fail-closed**：`job`/`work` 查詢對象不存在時 exit 1 並附建議命令，不得回傳空成功結果。
