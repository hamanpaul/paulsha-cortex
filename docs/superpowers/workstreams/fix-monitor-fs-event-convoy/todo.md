---
status: accepted
work_item: fix-monitor-fs-event-convoy
---

# fix-monitor-fs-event-convoy Todo

`#879`：**monitor 每個 inotify 事件起一條 Timer 執行緒在 store RLock 上排隊做完整 refresh**——事件速率
超過單次 refresh 處理時間時佇列無界成長。本機 2026-09-13 實測：59,114 條執行緒、RSS 7.8 GB＋kernel stack
0.9 GB（cgroup `memory.peak` 10.5 GB）、行程一天半平均 84% CPU；隨機抽 60 條執行緒全部 `futex` 等同一位址、
任一時刻只有一條 Running。重啟 `cortex-monitor.service` 後 54 條執行緒／63 MB，但事件一來又會堆回去。

## 現況查核（0913，對 main `739cde17`；runtime pin `79ba6447` 的 monitor 目錄與 main 零差異）

1. `paulsha_cortex/monitor/watcher.py:130-137`：`_DebouncedHandler._schedule` 每事件 `Timer.cancel()`＋新 Timer；
   已 fire 的 Timer 在 `_flush` → callback 內阻塞，cancel 對其無效 → 每個 debounce 視窗留一條阻塞執行緒。
2. `paulsha_cortex/monitor/service.py:280-301`：`_handle_fs_event` 非 project 路徑直接 `store.refresh()` 整份重掃；
   project 路徑 `_schedule_project_refresh`（`:317-330`）再起一條 Timer → `_flush_project_refresh`（`:332-342`）
   在 Timer 執行緒內 `refresh_project`＋`_install_watches`＋`_refresh_work_model`。
3. 監看面：project root（非遞迴）、`.git`（HEAD，非遞迴）、`.git/refs`（遞迴）；worktree 共用主 repo `.git`，
   任何 worktree 的 `git fetch`／`commit` 都會觸發。

## Current Sprint

- [ ] **RED**：`tests/test_monitor_fs_event_convoy_879.py` 依 spec R6 六項落地，對現行程式碼第 1／3／4 項為紅
- [ ] **實作**：依 design D1～D5——單一 `monitor-refresh` worker、有界 pending（`full`＋`project_ids`）、
      事件路徑只標記＋喚醒、worker 內以 `watch_debounce_ms` 合併、移除 per-project Timer 整組、
      `thread_count_warn_threshold`（預設 200）只警告且 60 s 節流、stop 後 worker 2 s 內退出
- [ ] **GREEN＋交付**：focused 三檔全綠；`changelog.d/fix-monitor-fs-event-convoy.md`；四件套入候選；
      PR `Closes #879`；新增檔無家目錄絕對路徑

## Handoff

- 驗收指令（worktree 內 PATH 裸命令）：
  `python3 -m pytest tests/test_monitor_fs_event_convoy_879.py tests/test_stage9_project_monitor_service.py tests/test_monitor_scan_health.py -q`
- 全套 gate 由 Manager 採信後執行；builder 不要去打 `.venv` 絕對路徑的命令。
- 部署後驗證（operator）：重啟 `cortex-monitor.service`，在 `~/prj_pri/paulsha-cortex-worktrees` 內連續
  `git fetch` 30 次，`ls /proc/$(systemctl --user show -p MainPID --value cortex-monitor.service)/task | wc -l`
  應維持在常駐數（約 60）±3。
