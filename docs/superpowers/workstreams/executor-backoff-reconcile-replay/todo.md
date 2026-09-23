---
status: accepted
work_item: executor-backoff-reconcile-replay
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Executor backoff 對帳 pending 重播補 ack 與 `_poll_workflow_job` 決策消費端（#946）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#946`。修 #928（`executor-backoff-terminal-admission`）R-C 對帳 seam 的兩個缺陷；[spec](../../specs/executor-backoff-reconcile-replay-spec.md)、[design](../../specs/executor-backoff-reconcile-replay-design.md)。
- 觸及模組（1 個 production 模組 → `domain_breadth: 0`）：`paulsha_cortex/coordinator/manager.py` 的 `_executor_backoff_admission_report`、`_executor_backoff_admission_decision`、`_poll_workflow_job`。`state_consistency: 1`：重播經 store 既有 exclusive lock 與冪等 merge，不新增 durable 狀態。
- 不改 `executor_backoff.py`（#850 store）、`registry.py`、slice lane（#929）、`classify_dispatch_result` 契約、`dispatch_reroute` 收據；不新增 CLI／persisted floor／tombstone；inventory 來源不變。
- spec／design／本 todo 文字是 pinned authority，只准翻 checkbox；澄清寫進 terminal reason。

## 現場證據

- 2026-09-21 pin `a8323abd`：claude/sonnet admission 永遠 `executor-backoff-unknown`（`inventory-pending`），verify／review 全卡；`_poll_workflow_job` 對 decision `KeyError: 'job_id'`。
- 現行 main：`reconcile_backoff` 回 PENDING 附 `missing_terminal_keys` 但 `manager.py` 無補寫路徑；`_merge_event` 冪等、`_fold_events` 對過期 deadline 歸 1 hits；`_poll_workflow_job` 直接 `next_job["job_id"]`。
- 本機 registry：claude/sonnet 22、claude/opus 28、luna 5、copilot 3 筆歷史 `rate_limited`／`quota` 終局。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_executor_backoff_reconcile_replay.py`（沿 `tests/test_executor_backoff_workflow_lane.py` 的 `_two_builder_identities`／fresh `JobRegistry`／`_ResumeDispatcher` 樣板），斷言逐條對應 spec R5 (a)–(e)；現行必須 RED（歷史終局 → unknown；poll → KeyError）。
- [x] **T2 source／重播（R1、R2、D1、D2）**：`_executor_backoff_admission_report` 對 PENDING identity 以 `executor_backoff.record_backoff` 逐筆重播 `missing_terminal_keys` 對應 inventory event（`now` 用 admission 時刻、`event_epoch` 用終局時間），單次重做 `reconcile_backoff`；`COMPLETE` → 既有 eligible／skipped 判定；否則 unknown。同 identity 一次 admission 只重播一次；store 回 UNKNOWN／`integrity-conflict`／`capacity-exceeded` 即停並記診斷；重播筆數 ≤ missing 筆數且 ≤ `MAX_RETAINED_EVENTS`。
- [x] **T3 source／診斷（R3、D3）**：unknown 元素加 `pending_count`、`earliest_event_epoch`、`latest_event_epoch`、`replayed_count`、`replay_diagnostics`（加法、既有鍵不變）；每次重播 `logger.info` 一行（executor／model_id／replayed_count／結果）。
- [x] **T4 source／poll 消費端（R4、D4）**：`_poll_workflow_job` 的 `dispatch_or_stop(updated)` 回傳經 `classify_dispatch_result`；`job` → `result["job_id"]`；`decision` → `result["dispatch_decision"]` 原樣、不 raise、不 needs_human、`attempts` 不變；`transition`／`none` 不加欄位；`ValueError` 不吞。
- [x] **T5 tests／回歸**：`tests/test_executor_backoff.py`、`tests/test_executor_backoff_workflow_lane.py`、`tests/test_dispatch_decision_contract.py`、`tests/test_provider_failure_recovery.py`、`tests/test_dispatch_runtime_preflight.py` 全綠、不改斷言；補「重播後真 cooldown 仍 skipped 且 deadline 不放寬」「第二次 admission `replayed_count == 0`」斷言。
- [x] **T6 documentation／changelog／CLI help**：新增 `changelog.d/executor-backoff-reconcile-replay.md` 並同步 `CHANGELOG.md [Unreleased]`；本票不新增 CLI，`cortex work --help` 輸出不變並以 help smoke 驗證；`docs/unified-work-lifecycle.md` 派工段「executor×model cooldown」補一句「pending 終局由 admission 重播補 ack，unknown 診斷含 pending／replayed 計數」；明寫 pin 升級前置：本票 merge 後才可再升含 #928 的 pin。
