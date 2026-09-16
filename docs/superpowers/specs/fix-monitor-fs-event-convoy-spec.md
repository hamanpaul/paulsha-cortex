---
status: accepted
work_item: fix-monitor-fs-event-convoy
---

# Spec: fix-monitor-fs-event-convoy

## Requirements

### R1. 缺陷敘述（defect narrative）

`#879`：`paulsha_cortex.monitor` 對**每個**檔案系統事件都起一條新執行緒去做 refresh（watcher 的
`_DebouncedHandler._schedule` 起 `threading.Timer` → `_flush` → `ProjectMonitorService._handle_fs_event`；
service 的 `_schedule_project_refresh` 再起一條 `threading.Timer` → `_flush_project_refresh`）。這些執行緒在
`SnapshotStore._lock`／`WorkReadModelStore._lock`（RLock）上排隊做整份 refresh（`store.refresh()`／
`refresh_project()` ＋ `_refresh_work_model()`），單次約 0.4～1 s。事件速率高於處理速率時（agent 在 repo
內建置、git fetch 改寫 `.git/refs/**`），佇列**無界成長**：本機實測 59,114 條執行緒、RSS 7.8 GB ＋
kernel stack 0.9 GB、行程常駐 84% CPU。這是 lock convoy，不是死鎖，log 無任何錯誤。

### R2. 現況根因證據（current-state evidence）

以 runtime pin `79ba6447`（monitor 目錄與 `origin/main` `739cde17` 零差異）為準：

1. `paulsha_cortex/monitor/watcher.py:130-137`（`_DebouncedHandler._schedule`）：每個事件 `cancel()` 舊
   Timer 並 `start()` 新 Timer；**已經 fire、正在 `_flush` 內阻塞的 Timer 不受 cancel 影響**，所以每個
   debounce 視窗都留下一條阻塞執行緒。
2. `paulsha_cortex/monitor/service.py:280-301`（`_handle_fs_event`）：路徑不屬任何 project root 時直接
   `self._publish_refresh(self._store.refresh())`（整份重掃）；屬 project root 時 `_schedule_project_refresh`
   → `service.py:317-330` 再起一條 Timer；`_flush_project_refresh`（`service.py:332-342`）在 Timer 執行緒內
   做 `refresh_project` → `_sync_project_roots` → `_install_watches` → `publish_events` → `_refresh_work_model`。
3. 實測：`sudo cat /proc/<pid>/task/<tid>/syscall` 隨機抽 60 條，全部 `futex(<同一位址>, …)` 無 timeout；任一
   時刻只有一條執行緒 Running，各消耗約 0.4 s CPU 後換下一條；事件停歇時佇列以約 110 條／分鐘消化。
4. 監看集合（inotify fdinfo 反查）：每個 workspace 的 project root（非遞迴）、`.git`（為 `HEAD`，非遞迴）、
   `.git/refs`（遞迴）。worktree 共用主 repo 的 `.git`，任何 worktree 內的 `git fetch`／`commit` 都會打進來。

### R3. 目標狀態

1. 任一時刻**最多一個** refresh 在執行；refresh 執行期間到達的事件只在一個有界的 pending 狀態上做標記
   （`full: bool` ＋ `project_ids: set[str]`），refresh 結束後再合併跑一輪。
2. 檔案事件路徑（watcher callback → service）**不再建立執行緒**：callback 為 O(1) 的「標記＋喚醒」。
3. `threading.active_count()` 在持續高事件速率下**有界**：上限為固定的常駐執行緒數（main、poll、rescan、
   github、refresh worker、watchdog observer／emitter）＋ 每個 watch path 至多一條 debounce Timer。
4. refresh 語意不變：project 事件仍走 `refresh_project` 路徑；非 project 路徑或 project root 不可用時仍走整份
   `store.refresh()`；events 仍經 `publish_events`；work model 仍在每輪 refresh 後 `_refresh_work_model(include_github=False)`
   一次（合併多個 project 時一輪只跑一次）。
5. `stop()`／`_shutdown()` 後 refresh worker 在 2 s 內退出，無殘留執行緒。

### R4. 範圍界線與明確排除

- **只改** `paulsha_cortex/monitor/service.py`（refresh worker、pending 狀態、移除 per-project Timer）與
  `paulsha_cortex/monitor/watcher.py`（`_DebouncedHandler` 維持每 watch path 一條 Timer 的 debounce，但
  callback 契約不變、不得阻塞）；新增／調整測試；新增 changelog 碎片。
- **不改**：監看範圍（`_watch_specs`）、`SnapshotStore`／`WorkReadModelStore` 的鎖結構、GitHub provider、
  `poll`／`rescan`／`github` 三條週期迴圈的語意、socket server。
- **不做**：`~/.agents/monitor/.work-items.snapshot.json.*.tmp` 殘檔清理（另開票）。
- **禁止**改動 `docs/superpowers/**` 的本 work item 四件套（hash 綁定的 planning input）。

### R5. 相容性

- `MonitorConfig.watch_debounce_ms` 語意保留：作為 worker 被喚醒後的合併等待時間（見 design D3）。
- 新增選填設定 `MonitorConfig.thread_count_warn_threshold: int = 200`：`threading.active_count()` 超過時
  `logger.warning` 一次（每 60 s 最多一次），**不**做任何 kill／drop。未宣告即預設值，現有設定檔不受影響。
- `StubWatcher`／`Watcher` Protocol 不變；既有 `tests/test_stage9_project_monitor_service.py`、
  `tests/test_monitor_scan_health.py`、`tests/test_stage9_project_monitor.py` 全部維持綠。

### R6. 驗證期待（verification expectations）

新增 `tests/test_monitor_fs_event_convoy_879.py`（unittest 風格，沿用 stage9 service 測試的
`StubWatcher`＋`_make_workspace` 建置方式），至少：

1. **convoy 不再發生**：把 `SnapshotStore.refresh`／`refresh_project` 以 `threading.Event` 擋住，連續觸發
   200 次 `stub_watcher.trigger(<project 內路徑>)` 與 50 次非 project 路徑；期間
   `threading.active_count()` 相對基線增量 ≤ 3；放行後 `refresh_project`＋`refresh` 總呼叫次數 ≤ 3
   （合併），且最後一輪之後 pending 為空。
2. **合併語意**：同一 project 多次事件 → 該 project 只 refresh 一次；混入非 project 路徑 → 整份 `refresh()`
   一次即可吸收（不必再逐 project refresh）。
3. **work model 每輪一次**：一輪合併 3 個 project 時 `_refresh_work_model` 只被呼叫 1 次。
4. **停機乾淨**：`service.stop()` 後 2 s 內 `threading.active_count()` 回到啟動前基線（扣除 daemon 的 watchdog
   執行緒），worker 執行緒 `is_alive()` 為 False。
5. **警告節流**：monkeypatch `threading.active_count` 回傳 999 時，60 s 內連續兩輪 refresh 只記一次 warning。
6. **既有行為**：`Stage9ServiceTests` 內事件驅動的 snapshot 更新測試維持綠（debounce 80 ms 內合併後仍會
   publish）。

focused 測試命令（builder 在 worktree 內用 PATH 裸命令執行）：
`python3 -m pytest tests/test_monitor_fs_event_convoy_879.py tests/test_stage9_project_monitor_service.py tests/test_monitor_scan_health.py -q`
全套 gate（`PSC_GATE_CMD_PYTEST`）由 Manager 採信後執行，builder 不需自跑。

### R7. 交付形式

- 第一張 build 卡 `git add` 本 work item 的四份 pinned planning 文件原樣入候選（不含 openspec）。
- changelog 碎片檔名固定為 `changelog.d/fix-monitor-fs-event-convoy.md`（archive gate 只認 `<work_item>.md`）。
- 新增檔案內容不得出現以家目錄開頭的絕對路徑（R-21 去識別化；用 `~/` 表示）。
- PR body 以 `Closes #879` 關聯 issue。
