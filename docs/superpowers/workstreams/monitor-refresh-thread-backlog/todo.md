---
status: accepted
work_item: monitor-refresh-thread-backlog
domain_breadth: 1
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Monitor refresh 有界排程、公平性與停止契約

## Boundary

- Issue：`hamanpaul/paulsha-cortex#827`。
- 修正 watcher／service 每事件 Timer、flush 進入同步 refresh 後的堆積，並提供
  refresh 排隊／執行／timeout／skip／stop 的診斷。允許調整 `monitor/work_api.py`
  refresh 排程／鎖邊界以滿足公平與停止契約；保留 snapshot 驗證、last-good 與
  fail-closed authority，不重寫 provider 業務語意。
- 不改 GitHub 節流／quota policy，不新增記憶體上限，不以重啟止血代替根因修正；
  不操作其他 instance 或同 Manager 的其他 repo 工作。
- 現況入口：`watcher._DebouncedHandler._schedule/_flush`、
  `ProjectMonitorService._schedule_project_refresh/_flush_project_refresh` 與
  `WorkModelRefresher.refresh`。後者目前持有全域鎖執行 provider scan，僅替換 Timer
  而不驗公平性不足以宣稱消除飢餓。

## Tasks

- [ ] **Intake source/tests/documentation**：以同 work_item 的 accepted spec/design
      明定 W/D/S、三模組 scope、stop publication fence 與測試 oracle；下列 #832
      已審查任務全部保留。現行 sizing 若 Red，等 #831 實際 runtime 重評，仍 Red
      先真拆，不降低 domain/state 或拿 Yellow 純函式 ready 覆蓋 band。
- [ ] 用固定、可查詢的 refresh worker 上限 W 取代每事件建立 Timer/thread；
      W 在測試中明確設定，執行中同一 project／refresh source 最多一份 pending dirty
      標記，無界 executor queue 不算有界實作。workspace 未映射事件的全量 scan
      也必須進入同一有界排程，不能另有同步事件旁路持續堆 thread。
- [ ] debounce 改為 coalesce 並保留最大等待期限 D，連續新事件不得無限向後推遲；
      active refresh 期間的新事件於其後補跑一次，重建最後狀態。不丟棄不同 project／
      source 的待更新標記；來源去重不以來源內最新一條事件代替所有 project 真相。
- [ ] 在 `tests/test_monitor_refresh_backlog.py` 寫可決定 RED：fake clock、
      `threading.Event` 屏障阻塞 project A 的首個 refresh；首個已開始後、放行前
      注入100個 A 事件。斷言 active≤W、A pending≤1；放行且不再注入事件後，
      A 的首輪＋補跑最多2次、最後 snapshot 含最後一次狀態。測實際使用的
      `refresh_project` 或 batch API，不只 spy 原路徑未呼叫的 `refresh_projects`。
- [ ] 另測不同 debounce 窗的合法更新可以分批執行；不得把任意時序100事件均≤2次
      寫成全稱契約。固定 seeds 的長事件串以虛擬60秒執行，查 component-owned
      worker／pending 數有界，避免用受其他測試影響的全域 active_count 作唯一斷言。
- [ ] 定義公平順序：local event／periodic poll／rescan／GitHub pending 均不能被
      同來源後來的事件無限插隊；每個已就緒來源最遲於當前有界工作結束加一輪
      既有就緒來源的服務時間內開始。測試持續 local event 下 GitHub 能開始、A
      熱點下 B 能開始，時限由 fake provider latency／D／W 推導，不依真實網路速度。
- [ ] slow provider 測試用可控制、遵守 timeout/cancel 的 fake：GitHub 卡住時
      local 更新仍於文件列出的 deadline 內執行或明確標示等待／timeout，不能回報
      虛假 freshness；適用時縮小全域鎖，只在 publication 持有。保留 generation／
      source revision 檢查，避免慢舊結果覆蓋較新的 snapshot。
- [ ] 對每個來源記錄 pending_since／started_at／last_success／failure reason 等
      可觀測資料；stale 僅表示時間狀態，另提供 queue-wait／provider-timeout／
      scheduler-skipped／refresh-failed 的具體原因。某來源成功不得抹掉其他來源
      尚未解除的錯誤；時間與既有 snapshot schema 的相容方式需附測試。
- [ ] stop 先拒收新事件並標記 generation 失效，取消 pending，再停止 watcher／
      scheduler。callback／provider 在 stop 前已開始的結果，stop 後不得發布事件
      或寫 durable snapshot；把檢查放在實際 commit/publish 邊界，不只 callback 入口。
- [ ] 測試 stop 期間仍有事件、callback 已開始、provider 正阻塞、unwatch/stop 重複
      呼叫等競態；合作式工作在明定 shutdown deadline S 內 join，之後無新 worker、
      pending 或 publication。各屏障以 bounded wait 收尾並於 finally 釋放，不留測試 thread。
- [ ] 不可取消的外部呼叫不宣稱已終止：保留最多 W 個既有 worker 的有界殘餘與
      stuck/timeout 診斷，不再替它建立新 thread；stop/generation fence 禁止遲到
      結果寫入／發布。若 provider 沒有可證實的有限 timeout，交回主流程處理 adapter
      邊界，不能用丟棄引用或更多 executor 規避驗收。
- [ ] 保留 `tests/test_stage9_project_monitor_service.py` 的 burst、rename、
      watch/unwatch、project recreate、Git control paths、rescan 與 last-good 回歸；
      補短 bounded real-thread smoke 驗證 owned threads 停止後回到基準，不呼叫
      GitHub API 或模型 CLI 作為單元測試。
- [ ] 補 changelog fragment／`CHANGELOG.md [Unreleased]` 與操作說明；透過 Cortex
      記錄 RED／GREEN、完整 gates、review、merge。部署驗收先保存現場，再在實際
      event 負載下量測 thread／queue／freshness 的有界性；任何服務重啟由主流程
      按當時 in-flight ownership 另外執行，不由本票測試擅自重啟共享 Manager。
- [ ] **documentation/CLI help 與 tests/真入口**：同步 monitor 操作說明的排程、
      timeout、stop 殘餘界線；在候選 checkout 外真跑 monitor --help 與
      `monitor --config <fixture> --once` JSON smoke。另以隔離 socket/config、fake
      provider 的 service harness 查 snapshot 並呼叫 stop，驗 owned worker 收尾；
      不發明 monitor status/stop CLI，不連 live socket。先 focused pytest，再 full pytest、既有 CI、
      PR-context policy、git diff --check；changelog fragment 必須 commit 後才算交付。
