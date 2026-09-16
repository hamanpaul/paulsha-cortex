---
status: accepted
work_item: monitor-refresh-thread-backlog
---

# Monitor refresh 有界排程規格

## Requirements

對應 [#827](https://github.com/hamanpaul/paulsha-cortex/issues/827)。本規格補足 #832 已審查的同 work_item todo，不刪減其驗收；觀測到的 thread/RSS 數字屬 issue 現場證據，不宣稱已在本 intake 重現。

1. **R1 有界 owner**：watcher/service 的檔案事件、未映射 workspace 全量 scan、poll/rescan/GitHub 都由有界排程承接；固定可查詢 worker 上限 W，不建立每事件 Timer 或無界 executor queue。
2. **R2 有界 coalesce**：每個 project/source 最多一份 pending dirty 標記；保持首次 pending 時刻與最大 debounce 等待 D，新事件不得無限延後。active A 已開始後到放行前的 100 事件合併為一次後續更新，首輪加補跑最多兩次，最後 snapshot 含最後狀態。不同 project 不互相覆蓋，不把任意時序 100 事件均最多兩次當全稱契約。
3. **R3 公平**：同來源後來事件不能插隊到已就緒其他來源前；local/poll/rescan/GitHub、熱點 A/非熱點 B 均需有依 W、D、有限工作耗時推導的開始上界。不能只改 Timer 而留下 provider 全域鎖飢餓。
4. **R4 Timeout 與 last-good**：慢 provider 的等待、timeout、skip、失敗需具體可見；沒有完成不得假裝 freshness。保留 snapshot 驗證、last-good、source owner/fail-closed authority 與 GitHub 壓力閘，遲到舊 generation/source revision 不得覆蓋新 snapshot。
5. **R5 診斷隔離**：各來源保留 pending_since、started_at、last_success、具體 failure reason；stale 是時間狀態而非根因。來源 A 成功不得清掉 B 未解除的錯誤；資料呈現必須相容既有 snapshot/read model。
6. **R6 Stop fence**：stop 先拒收及使 generation 失效，再取消 pending、停止 watcher/scheduler；已開始 callback/provider 的遲到結果不得在 stop 後發布事件或寫 durable snapshot。檢查與 commit/publication 必須同屬可線性化邊界，不可只在 callback 入口判斷。
7. **R7 有界收尾**：合作式工作於明定 S 內 join，重複 stop/unwatch 安全。不可取消呼叫最多留下既存 W 個有界殘餘，不宣稱已終止、不再增開替代 thread；保留 stuck/timeout 診斷與 stop fence。若所選 adapter 沒有可證明的有限 timeout，先回主流程處理依賴，不假裝驗收通過。
8. **R8 可重現驗收**：Event/barrier/fake clock 的 RED/GREEN、虛擬 60 秒 churn、跨來源公平、實際 publication fence 與短真 thread smoke；測試無 GitHub/model 呼叫，finally 釋放所有 gate。保留既有 burst/rename/watch/unwatch/recreate/Git-control/rescan/last-good 測試。

## Boundary

Production 規劃限定 `monitor/watcher.py`、`monitor/service.py`、`monitor/work_api.py` 的排程、診斷與 publication seam；不改 provider 業務規則、stale 門檻、GitHub quota/backoff、supervisor 或其他 instance。如需第 4 個 production 模組或新 adapter 協定，先重評/真拆。
本整包有 concurrency/atomicity，不是低風險單純 debounce 修正。Red 時只保留 accepted intake，不整包派 builder；#831 實際落地後重評，仍 Red 必須真拆。

## Evidence

基底 `60a3ffa867377b0c86fa2f10e91fb9820d91938c`：`watcher.py:130`、`service.py:317` 的 Timer；`service.py:279` 未映射事件同步 scan；`work_api.py:496` 的 refresh 在全域鎖內執行 provider scan。既有 `tests/test_stage9_project_monitor_service.py` 不足以單獨證明高 churn/stop race；測試 backlog 由同 work_item todo 完整列管。
