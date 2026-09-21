---
status: accepted
work_item: executor-backoff-slice-consumers
domain_breadth: 2
state_consistency: 0
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# Executor backoff slice lane admission 與 request／tick consumers 自有 OpenSpec tasks

本檔對應 accepted plan `docs/superpowers/plans/executor-backoff-slice-consumers.md` 的 candidate checkbox ledger。
本次 workflow card 已完成 accepted plan 的 T2–T8，以下 checkbox 與 landed 實作同步。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_executor_backoff_slice_lane.py`（沿 `tests/test_slice_executor_model.py` 的 fake launcher／
      `launcher_factory` 樣板），斷言逐條對應 #929「驗收」節；現行必須 RED（spec identity 在 cooldown 仍 launch、`launcher.model` 不存在、
      `dispatched[0]` IndexError）。
- [x] **T2 source／launcher**：新增公開 `SubprocessLauncher.model` property（回 `_model`），五個 `as_*` clone 行為不變；`autonomy` 與測試
      一律讀公開 property，不讀 private `_model`。
- [x] **T3 source／slice admission（R7、D4 後半）**：`autonomy.dispatch_ready` 把 identity 解析前移到 `_record_pending_slice`／建 worktree／
      launch **之前**；identity 依序 spec `executor`／`model_id` → `launcher.executor`＋`launcher.model`；任一 None → 不查、只留缺 identity
      診斷、不假造資格；`coordinator_root` 由 `dispatcher._registry._state_path` parent 導出（同 `manager` 終局分支的導法），拿不到 → 不查；
      命中 active cooldown → 不 `_record_pending_slice`、不建 worktree、不 launch、不建 job，slice 保持 dispatchable（不落 failed／
      needs_human）；store unknown → 同樣不 launch、sink 元素帶 unknown reason、不填假 deadline。
- [x] **T4 source／sink**：`dispatch_ready` 新增 keyword-only `backoff_skips: list[dict] | None = None`（元素
      `{"slice_id","executor","model_id","retry_after_epoch"}`，unknown 元素以 `reason` 區分且 `retry_after_epoch: null`）；回傳型別維持
      `list[dict]`；呼叫端以 signature 過濾傳入（比照 `manager_daemon._call_with_supported_kwargs`），既有固定簽名的 `dispatch_ready_fn`
      測試替身不受影響。
- [x] **T5 source／request 與 tick consumers**：`manager.run_tick` 結果加 `dispatch_skipped_by_backoff`（無命中 `[]`）；
      `manager.apply_slice_action` retry-build 分支 `dispatched` 空且 sink 非空 → 回 `{"slice_id","action","dispatch_skipped_by_backoff"}`
      而非 raise `retry-build-dispatch-failed`；`manager_daemon` dispatch request 取 `dispatched[0]` 前先判空，空且 sink 非空 → 回
      `{"slice_id","dispatch_skipped_by_backoff"}` 而非 `IndexError`；fanout／tick request 結果加同名欄位；known 與 unknown skip 可區分。
- [x] **T6 source／可觀測（R8）**：每次 skip `logger.info` 一行（slice_id／executor／model_id／retry_after_epoch 或 unknown 原因，測試以
      `caplog` 斷言）；不改 `cortex inspect status` 投影。
- [x] **T7 tests／回歸**：`tests/test_provider_failure_slice_lane.py`、`tests/test_slice_executor_model.py`、`tests/test_fix_slice_failed_deadend.py`、
      `tests/test_dispatch_decision_contract.py`、`tests/test_executor_backoff.py`、`tests/test_executor_backoff_workflow_lane.py` 全綠、
      不改斷言；真跑候選 CLI `--help`（`cortex status`／`stat`／`tick`；`dispatch` 只驗 help 相容，不執行）。
- [x] **T8 documentation／changelog／CLI help**：新增 `changelog.d/executor-backoff-slice-consumers.md` 並同步 `CHANGELOG.md [Unreleased]`；真跑 CLI `--help`（`cortex status`／`stat`／`tick`；`dispatch` 只驗 help 相容）確認輸出不變；`README.md` 的 request／tick 結果說明補 `dispatch_skipped_by_backoff` 欄位、known／unknown 區別與「未知餘量不等於
      額度足夠」；`docs/unified-work-lifecycle.md` slice lane 段補一句；明寫 #825 母票的 quota pool／forecast／reservation（R9）仍未完成、不因本票 merge 關閉。
