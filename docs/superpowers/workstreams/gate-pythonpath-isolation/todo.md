---
status: accepted
work_item: gate-pythonpath-isolation
---

# gate 測試不得繼承 daemon 的 PYTHONPATH

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1302`。
- 現況：gate 指令（`PSC_GATE_CMD_PYTEST`）與 job wrapper 的 `gate_ledger` 會繼承 Manager daemon 的 `PYTHONPATH`（指向 runtime pin）；wrapper 甚至明寫 `PYTHONPATH=<pin> python3 -m paulsha_cortex.coordinator.gate_ledger`。用子程序執行的測試會載入 pin 的程式，而不是被測 worktree 的程式。
- operator 端已把 env 改成 `env -u PSC_REPO_ROOT -u PYTHONPATH …` 暫時處理。本票要在程式裡修，不依賴 operator 設定。
- 不改 gate 的判定規則與門檻。

## Tasks

- [ ] **T1 RED**：測試在「pin 與 worktree 的 fixture 內容不同、daemon 環境帶 `PYTHONPATH=<pin>`」的情境下，gate 執行的測試載入的是 pin 的程式（現行行為，應失敗）。可參考 `tests/test_qualification_legacy_profile.py` 的 fixture 比對方式。
- [ ] **T2 gate 子程序清掉直譯器路徑變數**：gate 指令與 `gate_ledger` 執行測試時，一律移除 `PYTHONPATH`、`PYTHONHOME`、`PYTHONSTARTUP`、`PYTHONUSERBASE`（集中在一個 helper）。`gate_ledger` 本身需要載入 cortex 程式時，不得讓這些變數外洩到它啟動的測試子程序。
- [ ] **T3 preflight**：`PSC_PREFLIGHT_CMD` 的執行環境做同樣檢查與處理。
- [ ] **T4 文件**：operator 文件說明 gate 的環境隔離。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
