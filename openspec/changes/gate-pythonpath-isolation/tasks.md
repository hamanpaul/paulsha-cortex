---
status: accepted
work_item: gate-pythonpath-isolation
---

# Tasks

- [x] **T1 RED**：測試在「pin 與 worktree 的 fixture 內容不同、daemon 環境帶 `PYTHONPATH=<pin>`」的情境下，gate 執行的測試載入的是 pin 的程式（現行行為，應失敗）。可參考 `tests/test_qualification_legacy_profile.py` 的 fixture 比對方式。
- [x] **T2 gate 子程序清掉直譯器路徑變數**：gate 指令與 `gate_ledger` 執行測試時，一律移除 `PYTHONPATH`、`PYTHONHOME`、`PYTHONSTARTUP`、`PYTHONUSERBASE`（集中在一個 helper）。`gate_ledger` 本身需要載入 cortex 程式時，不得讓這些變數外洩到它啟動的測試子程序。
- [x] **T3 preflight**：`PSC_PREFLIGHT_CMD` 的執行環境做同樣檢查與處理。
- [x] **T4 文件**：operator 文件說明 gate 的環境隔離。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
