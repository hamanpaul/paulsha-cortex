---
status: accepted
work_item: copilot-probe-reap
---

# copilot 健康檢查程序逾時後連同子程序一起回收

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1297`。
- 現況：`paulsha_cortex/coordinator/executor_auth.py` 的 `_default_runner` 以
  `subprocess.run(argv, timeout=...)` 執行 `copilot -p 'Respond with OK only.' ...`。逾時時只會結束直接子程序
  （node wrapper），它啟動的 native `copilot-linux-x64` 程序會殘留。實機上已有多個殘留超過 8 天。
  `porcelain/bootstrap.py` 也有同類 probe。
- 不改 probe 的判讀語意（OK／unknown／失敗的分類）與逾時秒數。

## Tasks

- [x] **T1 RED**：以測試替身模擬「wrapper 啟動一個不會自己結束的子程序」，probe 逾時後斷言子程序仍存活
      （現行行為）。
- [x] **T2 整組回收**：probe 一律在新的 session／process group 執行（`start_new_session=True`）。逾時或例外時，
      對整個 group 先送 SIGTERM、短暫等待後送 SIGKILL，並等待回收，不留殭屍程序。`executor_auth.py` 與
      `porcelain/bootstrap.py` 的 probe 共用同一個 runner。
- [x] **T3 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
