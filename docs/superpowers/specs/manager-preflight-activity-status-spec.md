---
status: draft
work_item: manager-preflight-activity-status
issue: 1028
---

# Manager 長時間 PR preflight 狀態規格（#1028）

## Requirements

### 範圍與排程

本規格對應 live [#1028](https://github.com/hamanpaul/paulsha-cortex/issues/1028)，範圍是 Manager 同步執行 ship PR preflight 時的 status／健康訊號，以及該段工作延遲期間「job 已有終止收據、Manager 尚未採信」的可觀測性。#1028 於 2026-09-24 建立；issue body 與三則留言共同構成本規格來源。最新已發布版本為 [`v0.1.10`](https://github.com/hamanpaul/paulsha-cortex/releases/tag/v0.1.10)（2026-08-27）。依 issue owner 的排程留言，本票是可觀測性修正，不列為本輪 release blocker，也不改 preflight、review、card gate 或 merge authorization。

截至 2026-09-24 的即時核對，#885 仍 OPEN；其 [PR #1024](https://github.com/hamanpaul/paulsha-cortex/pull/1024) 仍 OPEN、尚未 merge，head 為 `dac3c4b8776dbae5eff0aa0002378f725e07967a`，check rollup 為空，並有兩條未解 Copilot review thread；GitHub 當下回報 mergeability `UNKNOWN`。#885 最新 09:26 UTC 留言記錄正式 run 已受理同一 Candidate 的 `retry-build` 並派出 builder 修兩項 finding 與 main 衝突；新 Candidate、verify、review、ship、PR merge 與 issue close 尚未完成。這些 #885／PR #1024 的 gates、review 與交付判斷仍由原 owner 負責；#1028 的 busy 或 exit-observed 狀態不能替它們產生通過或合併授權。

實作排在 #1024 的 `work_bridge.py` ship/preflight 路徑收斂並穩定之後；#1028 規劃可先完成。此票不依賴 #781 watcher I/O 或 #488 provider tool-loop progress。Issue comment 於 07:10 UTC 補充 #966/#965 已寫出 exit/log/gate 檔，但截至 07:09 UTC `cortex jobs` 仍顯示 `dispatched`；此處只要求 status 保留「觀察到 job process exit、Manager acceptance pending」的分離資訊，不替原 workflow owner 採信 gate、分類失敗或派後續工作。

Candidate aggregate `work_id` 為 `manager-preflight-activity-status`；producer/consumer IDs 仍是規劃候選。此 PR 不建立 WorkAuthority 記錄或 Cortex run。正式登記前須核對 canonical WorkAuthority 的唯一性與 authority binding，再 read back 登記結果。

### R1028.1 — Busy 必須由可驗證的長工作證據成立

Manager 啟動本 run 的 PR preflight 前，需發佈版本化 `cortex-manager-activity/v1` activity lease，綁定 exact Manager PID、repo、work_id、run_id、candidate head、operation=`ship-pr-preflight`、目前 stage、child PID、started_at 與 last_progress_at。preflight 執行期間，僅在明確 stage 前進或子程序輸出有新資料時更新 last_progress_at；不得用固定 timer 假造工作進度。完成、失敗或正常取消時，Manager 只能清除 operation_id 仍屬於自己的 lease。

活動記錄須由正式 Manager ship/preflight producer 寫入；普通 request、CLI、呼叫端或 stale status payload 不得設定 busy lease。activity 只能證明工作仍在執行，不證明 policy、pytest、review 或 ship 已通過。

### R1028.2 — Busy 與 stalled 分類 fail closed

`read_status()` 將 status 過期判為 busy 的條件必須同時成立：status daemon PID 存活；activity schema、owner PID、時間與 identity 欄位有效且一致；child PID 仍存活並屬於該 Manager 的 preflight process tree；`last_progress_at` 未超過既有 `STATUS_STALLED_AFTER_SECONDS` 門檻。

符合全部條件時，回傳 `degraded=false`、明確的 `activity.state=busy` 與 work identity/progress 時間，並將 `daemon.idle=false`。以下任一條件不成立，沿用既有 fail-closed stalled/dead/stale 結果：Manager 已死、child 消失或非其子孫、activity 缺失/格式錯誤/跨 PID、progress 超時、operation 身分不符。不得延長全域 120 秒門檻或永久把 busy 當健康。

舊 status JSON 無 activity 欄位時維持現行相容行為；不回填、不修改舊 status，也不根據 stale `in_flight` 清單推造目前工作身分。

### R1028.3 — 保留 job exit 與 Manager acceptance 的分界

長 preflight 延遲 Manager 輪詢時，status 必須保留兩種不同事實：目前 registry row（例如 `dispatched`）與從該 row 綁定的正式 exit receipt 觀察到的 process 終止。只有在 exact job identity、canonical receipt path 與 receipt 都能配對時，才可顯示 `process_exit_observed`、可得的 process exit code/time，以及 `manager_acceptance=pending`。資料不完整或無法配對時顯示 unknown／不附 exit claim，不以 PID 消失、日誌尾端或任意檔案推斷。

`exit_code=0`、gate 檔存在或 RED 測試資料都不代表 Manager 已採信 job、card gate 已通過或可派後續工作。此觀察只能唯讀投影，不能更新 registry、terminalize job、分類 gate、改變 run phase 或派工；這些動作仍由原 workflow owner 的現有 Manager polling／terminalization 路徑執行。

### R1028.4 — Operator status 投影

`cortex status` JSON 與人類可讀輸出需顯示 activity state、repo/work/run、PR/candidate、stage、started_at 與 last_progress_at；若有已配對的終止收據，另顯示 job ID、目前 registry state、觀察到的 process exit 與 `manager_acceptance=pending`。Busy 只描述 Manager 正執行該 preflight；不得標示任何 gate passed、改寫 gate evidence 或把舊 `last_tick_at` 冒稱為目前進度。

### R1028.5 — 保持容量與授權界線

busy status 仍須使 `porcelain.capacity_gate.evaluate_gate()` 對昂貴 spawn 回 `ask`；以 `daemon.idle=false` 維持既有判斷，不因 `degraded=false` 誤放行。除此之外不改 dispatch/merge authorization、preflight 命令/結果、PR review、ship audit 或 release gates。

### R1028.6 — 驗收矩陣

- 長度超過 120 秒、child/process identity 與 lease 都有效，且有持續 stage/output progress 的 preflight 顯示 busy、帶正確 identity/last progress、`degraded=false`；status 未聲稱 gate passed。
- 成功或失敗結束會清除同一 operation 的 lease；後續 status 恢復正常，failure counters 與 preflight result 仍由原路徑決定。
- Manager death、child exit/失聯、錯誤 parent PID、activity schema/identity tamper、progress 超時均顯示 degraded；stale lease 不得永久維持 busy。
- 精確綁定的 job exit receipt 在 Manager 尚未輪詢／採信時顯示 process exit observed + `manager_acceptance=pending`，保留 registry 原狀且不宣稱 card gate 通過；receipt 缺失、錯綁或未知時不推斷 exit。
- 以 busy status 呼叫 capacity gate 仍要求 operator ask；不增加 spawn 或 bypass 原容量 gate。狀態讀取不執行 terminalization 或後續派工。
- 舊 status schema 缺 activity 欄位仍可讀並保留原 age/liveness 分類；無 Manager activity 時不讀取外來/任意子程序來推定 busy。

### Non-goals

不變更 preflight 本身的判定、逾時或 CI 命令；不將 busy、process exit receipt 或 exit code 變成 gate 通過狀態；不替原 workflow owner 採信 job、分類 gate、推進 card/run 或派後續工作；不修 #885 的 archive/post-merge 行為；不處理 #781 多 instance I/O 或 #488 provider tool-loop。無 WorkAuthority/run 登記、live service 重啟、PR merge 或 runtime canary。
