---
status: accepted
work_item: executor-backoff-reconcile-replay
---

# Executor backoff 對帳 pending 重播補 ack 設計

## Decisions

### D1 重播用 store 既有公開 API，store 契約不動

`reconcile_backoff` 維持純讀（shared lock）；補 ack 由 Manager 在 `_executor_backoff_admission_report` 對 `status.reconciliation is PENDING` 的 identity 呼叫 `executor_backoff.record_backoff(coordinator_root, executor, model_id, now=now, outcome=event["outcome"], reset_at=event["reset_at"], reason=event["reason"], job_id=event["job_id"], event_epoch=event["event_epoch"])`，event 取自同一份 inventory 中 `terminal_key ∈ missing_terminal_keys` 的元素（terminal_key 由 store 對 `job_id`／`event_epoch` 導出，Manager 以 inventory 元素的 `job_id` 對應）。`_merge_event` 對相同 fingerprint 回 unchanged、對不同內容回 `integrity-conflict`；`_fold_events` 對「前一 deadline 已過」的事件把 hits 歸 1，歷史事件 fold 出過期 deadline，不會製造假 cooldown。不新增 persisted floor、tombstone 或 schema 欄位。

### D2 重播後單次重做 reconciliation，結果仍是三態

重播完成後對同 identity 再呼叫一次 `reconcile_backoff`（同 inventory）；`COMPLETE` → 走既有 eligible／skipped 判定；否則維持 unknown。不迴圈、不重試 provider；一次 admission 內同 identity 只重播一次（`inventory_cache` 旁加 `replay_cache`）。任一 `record_backoff` 回 `observation UNKNOWN`／`integrity-conflict`／`capacity-exceeded` 即停止該 identity 的重播並記診斷。

### D3 unknown 診斷擴充，decision 形狀向後相容

`unknown` 元素新增鍵皆為加法（`pending_count`、`earliest_event_epoch`、`latest_event_epoch`、`replayed_count`、`replay_diagnostics`），既有鍵與 `executor-backoff-unknown` decision 頂層形狀不變；`test_dispatch_decision_contract` 原斷言保留。

### D4 `_poll_workflow_job` 是 #830 的第六個消費端

`next_job = dispatch_or_stop(updated)` 後改為 `classified = classify_dispatch_result(next_job, registry=registry, run_id=run.run_id, before_phase=updated.current_phase)`；`kind == "job"` → `result["job_id"] = classified["job_id"]`；`kind == "decision"` → `result["dispatch_decision"] = next_job`；其餘不加欄位。`classify_dispatch_result` 的 `ValueError` 不吞。

### D5 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| 歷史終局 | fresh `JobRegistry` 塞 N 筆 `rate_limited` terminal（`exited_at` 過去）、空 store | admission eligible；store events == N、acks == N；第二次 admission `replayed_count == 0` |
| seam 漏記 | 終局後不呼叫 `record_executor_backoff_from_job` | 同上；hits 依事件時間 fold |
| 衝突 | store 先寫同 job_id 不同 reason | unknown、`inventory-conflict`、store 不變 |
| 容量 | monkeypatch `MAX_RETAINED_EVENTS` 小值 | unknown、`replay_diagnostics` 含 `capacity-exceeded` |
| 真 cooldown | 終局 `reset_at` 未來 | skipped、`retry_after_epoch` == deadline（重播不放寬） |
| poll 消費端 | `_ResumeDispatcher` 樣板，`dispatch_or_stop` 回 decision | 無 KeyError、`dispatch_decision` 原樣、無 `job_id`、`attempts` 不變 |

### D6 Sizing

1 個 production 模組 → `domain_breadth=0`；admission 路徑新增 store writer（重播），但寫入走 store 既有 exclusive lock 與冪等 merge、不新增 durable 狀態 → `state_consistency=1`；三件齊全時機械三維固定 4，總分 5／Yellow。
