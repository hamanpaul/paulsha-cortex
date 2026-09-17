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

# Executor backoff slice lane admission 與 request／tick consumers（child E＋F）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#929`。Parent：`executor-durable-backoff`／#825（母 [spec](../../specs/executor-durable-backoff-spec.md)、
  [design](../../specs/executor-durable-backoff-design.md)、[todo](../executor-durable-backoff/todo.md)），本票承接母 spec
  **R7 與 R8 的 slice／request／CLI 部分**；母 design D4 後半（slice 在副作用前查 store、request consumers 區別 launched／known skip／
  unknown skip）逐條生效。
- 兄弟：#850 `executor-backoff-store-core`（A：store）與 #928 `executor-backoff-terminal-admission`（C＋D＋parser），**兩者都 merge 後**
  才可 intake；本票只消費 store 的觀測與 #928 的 `record_executor_backoff_from_job`／admission helper，不改它們。
- 觸及模組（4 個 production 模組 → `domain_breadth: 2`）：`autonomy.py`（`dispatch_ready`）、`launcher.py`（公開 `model` property）、
  `manager.py`（`run_tick`、`apply_slice_action` retry-build 分支）、`manager_daemon.py`（dispatch／fanout／tick request 結果）。
  `state_consistency: 0`：本票是純消費端——不新增 durable 狀態、不寫 store（終局寫入已在 #928），只在單一 dispatch 內保證「命中即零副作用」
  的控制流順序；跨 process 一致性由 #850 負責。
- 不改 store（#850）、不改 parser／終局寫入／workflow admission（#928）、不動 GitHub `provider_backoff.py`；不改 slice row／handoff
  manifest schema、不改 `cortex inspect status`／`work_api` 投影（#821 另案），skip 證據只落 request／tick 結果與 manager log；
  不新增 override／clear CLI、不改 `spawn_admission` 節流語意、不改 `dispatch_ready` 回傳型別、不新增例外型別。

## 現場證據

- 母票 #825：slice lane `autonomy.dispatch_ready` 完全沒有退避——launch 前唯一的 provider 檢查是 `limiter.admit(...)`（spawn 最小
  間隔節流、無失敗記憶）；終局分類落到 `gate_reason = f"builder-failed-{outcome}"` 就結束；copilot `quota` 分類後下一 tick 仍派同一 identity。
- 現行 main：`autonomy.dispatch_ready` 在 `_record_pending_slice` **之後**才讀 spec `executor`／`model_id`；`launcher.py` 只有公開 `executor`
  property、`model` 為 private `_model`（`as_read_only`／`as_review_only`／`as_verdict_spool_writer`／`as_commit_required`／
  `as_write_forbidden` 五個 clone 都複製 `_model`）；`manager_daemon` dispatch request 直接取 `dispatched[0]`；`apply_slice_action`
  retry-build 分支 `dispatched` 空即 raise `retry-build-dispatch-failed`。
- 2026-09-17 實算：母票三件套 sizing 8／Red，依母 todo「#831 後實跑重評，仍 Red 真拆」拆出本票。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_executor_backoff_slice_lane.py`（沿 `tests/test_slice_executor_model.py` 的 fake launcher／
      `launcher_factory` 樣板），斷言逐條對應 #929「驗收」節；現行必須 RED（spec identity 在 cooldown 仍 launch、`launcher.model` 不存在、
      `dispatched[0]` IndexError）。
- [ ] **T2 source／launcher**：新增公開 `SubprocessLauncher.model` property（回 `_model`），五個 `as_*` clone 行為不變；`autonomy` 與測試
      一律讀公開 property，不讀 private `_model`。
- [ ] **T3 source／slice admission（R7、D4 後半）**：`autonomy.dispatch_ready` 把 identity 解析前移到 `_record_pending_slice`／建 worktree／
      launch **之前**；identity 依序 spec `executor`／`model_id` → `launcher.executor`＋`launcher.model`；任一 None → 不查、只留缺 identity
      診斷、不假造資格；`coordinator_root` 由 `dispatcher._registry._state_path` parent 導出（同 `manager` 終局分支的導法），拿不到 → 不查；
      命中 active cooldown → 不 `_record_pending_slice`、不建 worktree、不 launch、不建 job，slice 保持 dispatchable（不落 failed／
      needs_human）；store unknown → 同樣不 launch、sink 元素帶 unknown reason、不填假 deadline。
- [ ] **T4 source／sink**：`dispatch_ready` 新增 keyword-only `backoff_skips: list[dict] | None = None`（元素
      `{"slice_id","executor","model_id","retry_after_epoch"}`，unknown 元素以 `reason` 區分且 `retry_after_epoch: null`）；回傳型別維持
      `list[dict]`；呼叫端以 signature 過濾傳入（比照 `manager_daemon._call_with_supported_kwargs`），既有固定簽名的 `dispatch_ready_fn`
      測試替身不受影響。
- [ ] **T5 source／request 與 tick consumers**：`manager.run_tick` 結果加 `dispatch_skipped_by_backoff`（無命中 `[]`）；
      `manager.apply_slice_action` retry-build 分支 `dispatched` 空且 sink 非空 → 回 `{"slice_id","action","dispatch_skipped_by_backoff"}`
      而非 raise `retry-build-dispatch-failed`；`manager_daemon` dispatch request 取 `dispatched[0]` 前先判空，空且 sink 非空 → 回
      `{"slice_id","dispatch_skipped_by_backoff"}` 而非 `IndexError`；fanout／tick request 結果加同名欄位；known 與 unknown skip 可區分。
- [ ] **T6 source／可觀測（R8）**：每次 skip `logger.info` 一行（slice_id／executor／model_id／retry_after_epoch 或 unknown 原因，測試以
      `caplog` 斷言）；不改 `cortex inspect status` 投影。
- [ ] **T7 tests／回歸**：`tests/test_provider_failure_slice_lane.py`、`tests/test_slice_executor_model.py`、`tests/test_fix_slice_failed_deadend.py`、
      `tests/test_dispatch_decision_contract.py`、`tests/test_executor_backoff.py`、`tests/test_executor_backoff_workflow_lane.py` 全綠、
      不改斷言；真跑候選 CLI `--help`（`cortex status`／`stat`／`tick`；`dispatch` 只驗 help 相容，不執行）。
- [ ] **T8 documentation**：`README.md` 的 request／tick 結果說明補 `dispatch_skipped_by_backoff` 欄位、known／unknown 區別與「未知餘量不等於
      額度足夠」；`docs/unified-work-lifecycle.md` slice lane 段補一句；新增 `changelog.d/executor-backoff-slice-consumers.md` 並同步
      `CHANGELOG.md [Unreleased]`；明寫 #825 母票的 quota pool／forecast／reservation（R9）仍未完成、不因本票 merge 關閉。
