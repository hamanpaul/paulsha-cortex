---
status: accepted
work_item: tick-background-preflight
---

# periodic tick 內的 ship preflight 改為背景執行；同一 PR 不重複 preflight

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1320`（#1307 T2 未完成的部分）。
- 現況：#1307 的修正只把 action 為 `ship`、`regenerate-gates` 的 work-action control request 移到背景 thread（`manager_daemon._request_is_backgroundable`）。periodic tick 仍在主迴圈同步執行，tick 內會對每個 ship 階段的 run 依序呼叫 `preflight_ci --pr N`，每次都是完整 pytest，約 15 分鐘。2026-10-06 有一輪 tick 跑了 45 分鐘以上，期間同一個 PR 跑了兩次 preflight；CLI 的 rechain、resume 全部以 stalled 拒絕。
- 沿用 #1307 的背景執行機制（同一 run 的長時間工作互斥、結果回寫走 CAS），不改 preflight 的判準與結果語意。

## Tasks

- [ ] **T1 RED**：以假的耗時 preflight 模擬 periodic tick 內的 ship 階段 run，測試現行主迴圈會被擋住、status 不更新；並測試同一 PR、同一 head sha 在一輪 tick 內會跑兩次。
- [ ] **T2 背景 preflight**：periodic tick 內的 ship preflight_ci（以及其他會跑測試的步驟）交給背景 worker；tick 只負責排程與收割結果，每輪 tick 的時間有上限，status 持續更新。
- [ ] **T3 去重與退避**：同一輪 tick 內，同一 PR、同一 head sha 只跑一次 preflight。失敗時記錄結果，依退避規則在後續 tick 處理，不在迴圈內立即重跑。
- [ ] **T4 可觀測**：status 顯示進行中的背景工作（run、PR、head sha、開始時間），以及上一輪 tick 的耗時。
- [ ] **T5 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
