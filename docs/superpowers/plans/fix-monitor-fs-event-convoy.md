---
status: accepted
work_item: fix-monitor-fs-event-convoy
---

# fix-monitor-fs-event-convoy Plan

`#879`：monitor 每個檔案事件起一條 Timer 執行緒做完整 refresh，事件速率超過處理速度時無界累積
（本機 59k 執行緒、9.7 GB、84% CPU）。權威需求見 `docs/superpowers/specs/fix-monitor-fs-event-convoy-spec.md`，
設計裁決見 `-design.md`；本檔只列 Requirements 摘要、Decisions 摘要與 Tasks。

## Requirements（摘要，以 spec 為準）

- R3：任一時刻最多一個 refresh；事件路徑不建執行緒；`threading.active_count()` 有界；refresh 語意不變；停機乾淨。
- R4：只改 `monitor/service.py`、`monitor/watcher.py`、測試、changelog 碎片；不改監看範圍與鎖結構；禁改四件套。
- R5：新增 `thread_count_warn_threshold`（預設 200）只警告；既有 stage9／scan_health 測試維持綠。
- R6：新增 `tests/test_monitor_fs_event_convoy_879.py` 六項驗收。
- R7：碎片檔名 `changelog.d/fix-monitor-fs-event-convoy.md`；四件套入候選；無家目錄絕對路徑；PR `Closes #879`。

## Decisions（摘要，以 design 為準）

D1 單一 worker＋有界 pending；D2 事件路徑只標記；D3 worker 內以 `watch_debounce_ms` 合併；D4 watcher Timer 保留；
D5 只警告不干預；D6 週期迴圈不改；D7 範圍外項目。

## Scope（明確邊界）

- 允許：`paulsha_cortex/monitor/service.py`、`paulsha_cortex/monitor/watcher.py`、`paulsha_cortex/monitor/config.py`
  （只加 `thread_count_warn_threshold` 欄位與解析）、`tests/test_monitor_fs_event_convoy_879.py`（新）、
  必要時 `tests/test_stage9_project_monitor_service.py` 的既有斷言微調（若因 Timer 移除而依賴內部欄位）、
  `changelog.d/fix-monitor-fs-event-convoy.md`（新）。
- 禁止：其他 `paulsha_cortex/**`、`docs/superpowers/**` 四件套、systemd 模板、GitHub provider。

## Tasks

1. **RED**：新增 `tests/test_monitor_fs_event_convoy_879.py`，實作 spec R6 的 1～5 項；對現行程式碼跑
   focused 測試確認第 1、3、4 項為紅（第 1 項現行會讓 `active_count` 增量 ≫ 3）。
2. **config**：`MonitorConfig` 加 `thread_count_warn_threshold: int = 200` 與解析（比照 `watch_debounce_ms`
   的 intervals 表），預設值測試一條。
3. **service**：依 design D1～D3、D5 實作 worker 與 `_mark_refresh`；移除 `_schedule_project_refresh`、
   `_flush_project_refresh`、`_debounce_timers`、`_debounce_lock`、`_cancel_debounce_timers`；
   `run_forever`／`stop`／`_shutdown` 接上 worker 啟停。
4. **watcher**：確認 `_DebouncedHandler._flush` 的 callback 現在 O(1) 返回；不改 Protocol。
5. **GREEN**：focused 測試全綠（`python3 -m pytest tests/test_monitor_fs_event_convoy_879.py
   tests/test_stage9_project_monitor_service.py tests/test_monitor_scan_health.py -q`，在 worktree 內用 PATH 裸命令）。
6. **交付**：`changelog.d/fix-monitor-fs-event-convoy.md`（一段 zh-tw：症狀、根因、修法、新設定欄位）；
   第一張 build 卡 `git add` 四份 pinned planning 文件；PR body `Closes #879`；新增檔以家目錄絕對路徑掃描零命中。
