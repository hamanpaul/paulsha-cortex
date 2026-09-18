---
status: accepted
work_item: executor-backoff-terminal-admission
---

# Executor backoff terminal 記錄與 workflow admission 設計（child C＋D＋parser）

## Decisions

### D1 Parser 是純函式、authority 不升級（母 D3）

`parse_reset_hint` 只回 int／None，內部解析 metadata（timezone／base-year／source）進診斷不進 provider_outcome 形狀。結構化 `resetsAt` 與文字互斥時保留結構化。「該年已過」與「無可信年份的年末→年初」都回 None，交 store 走本機保守退避；不猜 DST、不猜 timezone。文字命中不改 `classification.authority`，只填既有 `reset_at`。

### D2 終局寫入是 store 的 caller，不重做 fold

`record_executor_backoff_from_job` 只負責資格判定與 identity／terminal-time 提取，把 immutable event 交給 #850 的 `record_backoff`；event-time fold、冪等、deadline 政策全部在 store。兩條 lane 的終局分支各自導出 `coordinator_root`（slice：`registry._state_path` parent；workflow：`resume_workflow_run` 參數）後呼叫同一 helper。helper 對任何缺失都回 None 而非 raise，終局收斂路徑不因退避記錄失敗而中斷。

### D3 Unknown 是第三態，admission 不得把它折成 allow／deny

admission 前先取 registry 既有持久終局作 inventory 餵 store reconciliation；store 回 unknown 時，workflow admission 回 `executor-backoff-unknown` 決策，附診斷、無 `retry_after_epoch`。與 active cooldown（`executor-backoff`，帶 min deadline）用不同 reason，讓 operator／read model 可區分「等到期」與「store 需要人看」。兩者都沿 #830 decision 契約：不落 needs_human、不建 worktree／job、不計 `attempts`。

### D4 Candidate 前置過濾，不經 snapshot_lookup

`subagent-build` 只宣告 `module:pytest`，`run_runtime_preflight` 只對 provider 類 capability 呼叫 `snapshot_lookup` 且 provider_id 只解析到 executor（分不出 `codex/spark` 與 `codex/luna`），故退避以 candidate 前置過濾實作：`_executor_backoff_filter` 回 `(eligible, skipped)`，順序與 domain／pin／independence 規則不變；`_runtime_preflight_gate` 新增 `candidates=None`（None 維持既有 `_workflow_identity_candidates`）；`_select_workflow_identity` 取 `eligible[0]`。`_provider_failure_reroute` 先套同一過濾再交 `evaluate_dispatch_gate`。#205 run-scoped override 的單元素清單被命中時不替換、不放寬，回 `executor-backoff`。

### D5 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| parser／時區 | `TZ=Asia/Taipei`＋`tzset()`、aware fake now | Codex 訊息→正確 epoch；同日已過→None；年末→年初→None；Retry-After 0／120／負值／非有限 |
| 分類接線 | 結構化 `resetsAt` 事件 vs spark 純文字 | 結構化優先；文字回 `rate_limited`／`TEXT_SIGNAL` 且 `reset_at` 非 None；`to_dict()` 鍵不變 |
| 終局寫入資格 | 兩 lane 終局 fixture | 只 `rate_limited`／`quota` 且非 hint 寫入；缺 identity 回 None＋診斷；同終局重送不增 hits（store 保證，測試釘住） |
| workflow admission | `_two_builder_identities`＋fresh `JobRegistry`＋清 `_EXECUTOR_AUTH_CACHE` | 第一候選 cooldown → 派第二候選＋`dispatch_reroute`；全 cooldown → `executor-backoff`、無新 job、`attempts` 不變、無 needs_human；到期可派回；override 命中 → `executor-backoff` |
| unknown | corrupt store fixture | `executor-backoff-unknown`、無假 deadline、不 launch、不耗 retry |
| #830 契約 | `resume_workflow_run` 兩個 retry 分支 | decision 原樣回傳、不造 job_id |

### D6 Sizing 與後續

3 個 production 模組 → `domain_breadth=1`；不新增 durable 狀態、跨 process 一致性由 #850 負責 → `state_consistency=1`；三件齊全時機械三維固定 4，總分 6／Yellow。slice lane 與 request consumers 由 #929 承接；quota pool／forecast／reservation（母 R9）不在本票。
