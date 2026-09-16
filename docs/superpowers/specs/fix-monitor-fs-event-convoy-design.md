---
status: accepted
work_item: fix-monitor-fs-event-convoy
---

# Design: fix-monitor-fs-event-convoy

## Decisions

### D1. 單一 refresh worker ＋ 有界 pending 狀態

`ProjectMonitorService` 新增：

```python
self._refresh_lock = threading.Lock()          # 保護下面三個欄位
self._pending_full = False
self._pending_projects: set[str] = set()
self._refresh_wakeup = threading.Event()
self._refresh_thread: threading.Thread | None = None   # daemon，名稱 "monitor-refresh"
```

`run_forever()` 在啟動 poll／rescan／github 執行緒的同一處啟動 worker；`stop()`／`_shutdown()` set
`_stop_event` 並 `_refresh_wakeup.set()` 讓 worker 立即退出，`_shutdown()` 以 `join(timeout=2.0)` 等待。

### D2. 事件路徑只做標記

`_handle_fs_event(path)` 改為：

- `project_id = self._project_id_for_path(path)`；project root 不可用（現行 `checked_stat_mode` 判斷）或
  `project_id is None` → `_mark_refresh(full=True)`；否則 `_mark_refresh(project_id=project_id)`。
- `_mark_refresh` 在 `_refresh_lock` 內更新 `_pending_full`／`_pending_projects` 後 `_refresh_wakeup.set()`。
  全程 O(1)，不呼叫 store／server／work model，不建立執行緒。

`_schedule_project_refresh`／`_flush_project_refresh`／`_debounce_timers`／`_debounce_lock`／
`_cancel_debounce_timers` 整組移除（debounce 由 D3 的 worker 合併等待取代）。

### D3. worker 迴圈與 debounce 合併

```
while not stop:
    wakeup.wait(); wakeup.clear()
    if stop: break
    stop_event.wait(watch_debounce_ms / 1000)      # 合併同一視窗內的後續事件
    with refresh_lock: full, projects = take-and-clear pending
    if full:  events = store.refresh()
    else:     events = tuple(e for pid in sorted(projects) if (e := store.refresh_project(pid)) is not None)
    sync_project_roots(); install_watches()
    if events: server.publish_events(events)
    refresh_work_model(include_github=False)
    warn_if_thread_count_high()
```

任一時刻最多一個 refresh 在跑；refresh 期間到達的事件只改 pending，下一輪合併處理。例外處理沿用現行
`_refresh_work_model` 的 try/except；`store.refresh*` 若拋例外，worker 記 `logger.exception` 後**繼續迴圈**
（不得讓 worker 死掉導致 monitor 永久停更）。

### D4. watcher 層的 debounce Timer 保留

`_DebouncedHandler` 仍是每個 watch path 一條 Timer（cancel 舊、起新），這是有界的（watch path 數 × 1）。
`_flush` 的 callback 現在是 O(1) 標記，Timer 執行緒立刻結束，不再累積。`WatchdogFileWatcher` 其餘不動。

### D5. 自我保護只警告不干預

`warn_if_thread_count_high()`：`threading.active_count() > config.thread_count_warn_threshold` 時
`logger.warning("monitor thread count %d exceeds %d", ...)`，以 `time.monotonic()` 節流每 60 s 一次。
不 drop 事件、不 kill 執行緒——本設計的有界性來自 D1～D3，警告只是回歸偵測。

### D6. 週期迴圈不改

`_poll_loop`／`_rescan_loop`／`_github_refresh_loop` 維持各自執行緒與現行呼叫（它們固定週期、有界）。
它們與 worker 共用 store／work model 的既有 RLock，屬既有行為，本票不重構鎖結構。

### D7. 不在本設計範圍

縮小 `_watch_specs` 監看面、`.tmp` 殘檔清理、GitHub provider 節流、server 每連線一條執行緒（有 fd 上限自然有界）。
