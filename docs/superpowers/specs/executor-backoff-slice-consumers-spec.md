---
status: accepted
work_item: executor-backoff-slice-consumers
---

# Executor backoff slice lane admission 與 request／tick consumers 規格（child E＋F）

## Requirements

對應 [#929](https://github.com/hamanpaul/paulsha-cortex/issues/929)，承接母 [spec](executor-durable-backoff-spec.md) R7 與 R8 的 slice／request／CLI 部分；store（#850）與 parser／終局寫入／workflow admission（#928）由兄弟票定義，本票只消費、不改。

1. **R-A Slice admission（母 R7、design D4 後半）**：`autonomy.dispatch_ready` 在 `_record_pending_slice`／建 worktree／launch 之前解析 identity 並查 store；identity 依序 spec `executor`／`model_id`，否則 `launcher.executor`＋新增公開 `launcher.model` property（不讀 private `_model`）；任一 None → 不查、只留缺 identity 診斷、不假造資格；`coordinator_root` 由 `dispatcher._registry._state_path` parent 導出，拿不到 → 不查。命中 active cooldown → 不 `_record_pending_slice`、不建 worktree、不 launch、不建 job，slice 保持 dispatchable；store unknown → 同樣不 launch、另附 unknown 診斷、不填假 deadline。
2. **R-B Sink 與型別**：`dispatch_ready` 新增 keyword-only `backoff_skips: list[dict] | None = None`（元素 `{"slice_id","executor","model_id","retry_after_epoch"}`，unknown 元素以 `reason` 區分、`retry_after_epoch: null`）；回傳型別維持 `list[dict]`、不新增例外型別；呼叫端以 signature 過濾傳入（比照 `manager_daemon._call_with_supported_kwargs`），既有固定簽名的 `dispatch_ready_fn` 測試替身不受影響。
3. **R-C Request／tick consumers**：`manager.run_tick` 結果加 `dispatch_skipped_by_backoff`（無命中 `[]`）；`manager.apply_slice_action` retry-build 分支 `dispatched` 空且 sink 非空 → 回 `{"slice_id","action","dispatch_skipped_by_backoff"}` 而非 raise `retry-build-dispatch-failed`；`manager_daemon` dispatch request 取 `dispatched[0]` 前先判空，空且 sink 非空 → 回 `{"slice_id","dispatch_skipped_by_backoff"}` 而非 `IndexError`；fanout／tick request 結果加同名欄位；launched／known skip／unknown skip 三者可區分。
4. **R-D 可觀測（母 R8）**：每次 skip `logger.info` 一行；文件同步 `dispatch_skipped_by_backoff` 欄位、known／unknown 區別與「未知餘量不等於額度足夠」；真跑候選 CLI `--help`（`cortex status`／`stat`／`tick`；`dispatch` 只驗 help 相容）。
5. **R-E 測試**：`tests/test_executor_backoff_slice_lane.py` 的 RED→GREEN；既有 slice lane／executor model／failed deadend／decision contract／#850／#928 測試原斷言保留；fake launcher＋registry／worktree spies，不連真 daemon／provider。

## Boundary

Production 涵蓋 `autonomy.py`、`launcher.py`、`manager.py`（`run_tick`、`apply_slice_action`）、`manager_daemon.py` 四個模組。不改 store（#850）、不改 parser／終局寫入／workflow admission（#928）、不動 GitHub `provider_backoff.py`；不改 slice row／handoff manifest schema、不改 `cortex inspect status`／`work_api` 投影（#821）；不新增 override／clear CLI、不改 `spawn_admission`、不改 `dispatch_ready` 回傳型別。本票 merge 不關閉 #825。

## Evidence

母票 #825：slice lane 完全沒有退避（launch 前只有 `limiter.admit` 節流）、終局分類落到 `builder-failed-{outcome}` 就結束；現行 main `dispatch_ready` 在 `_record_pending_slice` 之後才讀 spec identity、`launcher` 只有公開 `executor`、`manager_daemon` 直接取 `dispatched[0]`、`apply_slice_action` 空 dispatched 即 raise；2026-09-17 母票實算 sizing 8／Red，依母 todo 拆出。
