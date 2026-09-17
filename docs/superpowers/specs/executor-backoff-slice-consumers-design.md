---
status: accepted
work_item: executor-backoff-slice-consumers
---

# Executor backoff slice lane admission 與 request consumers 設計（child E＋F）

## Decisions

### D1 副作用前查詢，命中即零副作用

`dispatch_ready` 的 identity 解析與 store 查詢移到 `_record_pending_slice`、worktree 建立、launch、job 建立之前；命中或 unknown 時直接 `continue`，slice row 不變（仍 dispatchable，不落 failed／needs_human）。這是控制流順序的不變量，不新增任何 durable 狀態，跨 process 一致性由 #850 的 store 負責。

### D2 Identity 來源與缺席

先 spec `executor`／`model_id`，否則 `launcher.executor`＋新增的公開 `SubprocessLauncher.model` property。五個 `as_*` clone 已複製 `_model`，公開 property 只是讀取面，不改 clone 語意。任一 None → 不查、記缺 identity 診斷、照舊派工（不假造資格，也不因此阻擋）。

### D3 Sink 而非例外，signature 過濾而非改簽名

`backoff_skips` 是 keyword-only sink；`dispatch_ready` 回傳型別不變、不新增例外型別。呼叫端（`run_tick`、`apply_slice_action` retry-build、`manager_daemon` dispatch／fanout／tick）各自建 sink 並以 signature 過濾傳入，既有固定簽名的 `dispatch_ready_fn` 測試替身不受影響。known skip 帶 `retry_after_epoch`，unknown skip 帶 `reason` 且 `retry_after_epoch: null`。

### D4 Request consumers 的空列表安全

`dispatched` 為空不再是錯誤：sink 非空 → 回 `dispatch_skipped_by_backoff` 結果（retry-build 不 raise、dispatch request 不 IndexError）；sink 也空 → 維持既有行為。不改 slice row／handoff manifest schema、不改 `cortex inspect status` 投影，證據只落 request／tick 結果與 manager log。

### D5 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| slice admission | fake launcher＋registry／worktree spies＋tmp store | 命中 → `launch` 未呼叫、無 job、無 worktree、row 不變、sink 含 deadline；到期 → 正常 launch |
| identity fallback | spec 無宣告 | 用 `launcher.executor`＋`launcher.model`；任一 None → 不查照舊派工＋診斷 |
| unknown | corrupt store fixture | 不 launch、sink 元素帶 unknown reason、無假 deadline |
| retry-build | `apply_slice_action(action="retry-build")` | 回 `dispatch_skipped_by_backoff`，不 raise |
| daemon request | control request harness | dispatch 回 skip 結果不 IndexError；fanout／tick 帶同名欄位 |
| 可觀測 | `caplog` | 每次 skip 一行；CLI `--help` 相容 |

### D6 Sizing 與後續

4 個 production 模組 → `domain_breadth=2`；純消費端、無 durable 狀態 → `state_consistency=0`；三件齊全時機械三維固定 4，總分 6／Yellow。依賴 #850 與 #928 先 merge。quota pool／forecast／reservation（母 R9）仍未完成。
