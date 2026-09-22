---
status: accepted
work_item: executor-backoff-reconcile-replay
---

# Executor backoff 對帳 pending 重播補 ack 與 `_poll_workflow_job` 決策消費端規格

## Requirements

對應 [#946](https://github.com/hamanpaul/paulsha-cortex/issues/946)，修 #928（[spec](executor-backoff-terminal-admission-spec.md) R-C 對帳 seam）落地後暴露的兩個缺陷；#850 store 語意（[spec](executor-backoff-store-core-spec.md)）不重述、不改。

1. **R1 pending 必須有出口**：admission 前的 reconciliation 回 `PENDING`（inventory 有 terminal、store 無對應 event／ack）時，Manager 必須以 store 公開 `record_backoff` 把 `missing_terminal_keys` 對應的 inventory event 逐筆重播補 ack，再對同一 identity 重做一次 reconciliation；重播後 `COMPLETE` → 依 `active_backoff` 正常判 eligible／skipped；仍 `PENDING`／`UNKNOWN` 或 store 回 `integrity-conflict`／`capacity-exceeded` → 維持 `executor-backoff-unknown`，不得折成 allow。歷史終局（早於 store 建立、永遠不會被 `record_executor_backoff_from_job` 記錄）與 seam 漏記終局（終局與寫入之間崩潰）走同一機制，不另設 epoch 門檻、不新增 persisted floor／tombstone。
2. **R2 重播是冪等且事件時間為準**：重播送入的 `outcome`／`reset_at`／`reason`／`job_id`／`event_epoch` 與 inventory event 完全一致（fingerprint 相同 → store 回 unchanged，不增 hits）；`now` 用 admission 當下時刻、`event_epoch` 用終局 immutable time，deadline 由 store 以事件時間 fold（歷史事件 fold 出過期 deadline → 不 active）。每次 admission 重播上限為 inventory 中 missing 的筆數，且不得超過 store `MAX_RETAINED_EVENTS`；超限回 unknown 附診斷。
3. **R3 unknown 診斷可判讀**：`executor-backoff-unknown` 的每個 `diagnostics` 元素在既有 `executor`／`model_id`／`diagnostics`／`reconciliation`／`evidence_refs` 之外加 `pending_count`、`earliest_event_epoch`、`latest_event_epoch`（無 pending → 0／null）與 `replayed_count`、`replay_diagnostics`（未重播 → 0／`[]`），讓 operator 分得出「歷史殘留已補」「真有未合併新終局」「store 拒絕」。
4. **R4 `_poll_workflow_job` 接 #830 契約**：卡片通過推進後 `dispatch_or_stop(updated)` 的回傳經 `classify_dispatch_result` 投影：`job` → `result["job_id"]`；`decision`（含 `executor-backoff`／`executor-backoff-unknown`／`runtime-preflight-*`／`needs-decomposition`）→ `result["dispatch_decision"] = <decision dict 原樣>`，不取 `job_id`、不 raise、不落 needs_human、`attempts` 不變；`transition`／`none` → result 不加欄位。任何 `ValueError`（契約違反）維持既有例外路徑。
5. **R5 測試**：`tests/test_executor_backoff_reconcile_replay.py` RED→GREEN：(a) registry 內既有 `rate_limited`／`quota` 終局＋空 store → admission 後 identity eligible、store 含重播 event、再 admission 不再重播；(b) 終局後崩潰（store 缺 event）→ 同 (a)；(c) inventory event 與 store 已有 event 衝突 → unknown、`inventory-conflict`、不覆寫；(d) 重播 store 回 capacity-exceeded → unknown 附診斷；(e) `_poll_workflow_job` 收 decision 不 KeyError、`dispatch_decision` 原樣、無 job_id。既有 `tests/test_executor_backoff.py`、`tests/test_executor_backoff_workflow_lane.py`、`tests/test_dispatch_decision_contract.py`、`tests/test_provider_failure_recovery.py` 原斷言保留。

## Boundary

Production 只改 `paulsha_cortex/coordinator/manager.py`（`_executor_backoff_admission_report`／`_executor_backoff_admission_decision`／`_poll_workflow_job`）；`executor_backoff.py`（#850）與 `registry.py` 不改；不新增 CLI、不改 `dispatch_reroute` 收據形狀、不改 slice lane（#929）、不改 `classify_dispatch_result` 四類契約。inventory 來源維持 registry 既有持久終局（`registry://job-terminal-inventory`），不掃 live state 以外來源。

## Evidence

2026-09-21 22:03 CST pin `a8323abd`：`read_store` VALID、四個 identity `active_backoff` 皆 None，但 claude/sonnet admission 回 `{"reason":"executor-backoff-unknown","diagnostics":[{"reconciliation":"pending","diagnostics":["inventory-pending"],"evidence_refs":["registry://job-terminal-inventory"]}]}`；本機 registry 有 claude/sonnet 22 筆、claude/opus 28 筆歷史 `rate_limited` 終局（皆早於 store 建立）。同 pin `_poll_workflow_job` 對 decision `KeyError: 'job_id'` → run `resume-workflow-failed`。現行 main `reconcile_backoff` 只讀（shared lock）回 PENDING，`manager.py` 無任何路徑補寫 missing terminal。pin 已回退 `442fe23f`。
