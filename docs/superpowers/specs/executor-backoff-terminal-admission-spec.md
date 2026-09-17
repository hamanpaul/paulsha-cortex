---
status: accepted
work_item: executor-backoff-terminal-admission
---

# Executor backoff terminal 記錄、reset hint 解析與 workflow lane admission 規格（child C＋D＋parser）

## Requirements

對應 [#928](https://github.com/hamanpaul/paulsha-cortex/issues/928)，承接母 [spec](executor-durable-backoff-spec.md) R4 parser 部分、R5、R6、R3 消費端與 R2 尾段對帳 seam；store 語意（R1／R2 主體／R3 store 端／R4 期限政策）由 #850 定義，本票不重述、不改。

1. **R-A Reset hint 解析（母 R4 parser、design D3）**：`parse_reset_hint(text, *, now, tz=None) -> int | None`。Retry-After 非負秒 → `now + seconds`（0 → now，margin 由 store 加）；Codex 月日／AM-PM 只依明示 timezone 與基準年解析，該年所得時刻已過 → None、不滾到明年；負值／非有限／溢位／無訊號／模糊 DST → None。timezone／now seam 可注入 deterministic fixture，不依 CI host 時區。接進 `classify_provider_failure`：文字命中 `RATE_LIMIT`／`QUOTA` 時填既有可選欄 `reset_at`，結構化 `resetsAt` 優先；不改 `authority`、不新增 outcome vocabulary、`to_dict()` 鍵集合與 provider_outcome 四必要 keys＋可選 `reset_at` 形狀不變。
2. **R-B 終局寫入資格（母 R5）**：單一 helper `record_executor_backoff_from_job(coordinator_root, job, classification) -> ExecutorBackoff | None`，slice／workflow 兩條 lane 的終局分類分支共用；只接受 `rate_limited`／`quota` 且 `authority != hint`；以終局 job 的真 `executor`／`model_id`／`job_id` 與 immutable terminal time 送 store 的 `record_backoff`；缺欄位、`coordinator_root is None`、outcome 不符 → 回 None、不造 key、不炸終局收斂、留診斷。
3. **R-C 對帳 seam（母 R2 尾段、R3 消費端）**：admission 前以 registry 既有持久 job 終局作 caller inventory 餵 store 的 reconciliation 入口；store 回 unknown（corrupt／unreadable／未 ack pending intent）時 admission 回 unknown 決策，不當空檔、不捏造 deadline、不耗 provider retry；有效 store 恢復後可重新 admission。不新增 registry writer、不掃 live state 以外來源。
4. **R-D Workflow admission（母 R6、design D4 前半）**：`_executor_backoff_filter(candidates, *, coordinator_root, now) -> (eligible, skipped)`，順序不變、不放寬 domain／pin／independence；`_dispatch_workflow_card` 在 `_runtime_preflight_gate` 與 `_select_workflow_identity` 前套用，lookup 對 cooldown identity 投影 `ProviderFreshness(status="degraded", source="executor-backoff")`；`_provider_failure_reroute` 套同一過濾。全候選 cooldown → `{"run_id","current_phase","reason":"executor-backoff","retry_after_epoch":<min deadline>,"skipped":[...]}`（含 #205 run-scoped override 單元素命中），不落 needs_human、不建 worktree／job、不耗 `provider-retry:<card>`；候選中有 unknown → `reason: "executor-backoff-unknown"` 附診斷，不把已知最早期限當全體可恢復時間。決策沿 #830 `classify_dispatch_result` 的 `decision` 契約在 `resume_workflow_run` 原樣回傳、`attempts` 不變。
5. **R-E 證據**：過濾後派出的 job 帶 `dispatch_reroute = {"source": "executor-backoff", "skipped": [...]}`；每次 skip `logger.info` 一行（executor／model_id／retry_after_epoch 或 unknown 原因）。
6. **R-F 測試**：`tests/test_reset_hint_parsing.py`、`tests/test_executor_backoff_workflow_lane.py` 的 RED→GREEN；既有 provider backoff／failure recovery／outcome／runtime preflight／decision contract／#850 store 測試原斷言保留；不使用真 provider／模型／daemon。

## Boundary

Production 涵蓋新 `reset_hint.py`（或併入 `outcome_taxonomy`）、`provider_outcome.py`、`manager.py` 三個模組；`registry.py` 只在 job 未知欄位被 fail-closed 拒絕時放行 `dispatch_reroute`。不改 `executor_backoff.py` store 語意（#850）、不動 GitHub `provider_backoff.py`、不改 slice lane 的 `autonomy`／`launcher`／`manager_daemon` 與 `run_tick`／`apply_slice_action` 的 `dispatch_skipped_by_backoff`（#929）。不新增 override／clear CLI、不改 `spawn_admission`、不改候選順序、不做 quota pool／forecast／reservation（母 R9）。本票 merge 不關閉 #825。

## Evidence

母票 #825 現象與根因（spark 連燒三次、claude `reset_at` 無消費者、copilot quota 次 tick 再派）；現行 main `_provider_failure_reroute` in-memory 單卡有界、`_RESET_AT_KEYS` 只解析結構化 reset、`reset_at` 為既有可選欄；2026-09-17 母票三件套實算 sizing 8／Red，依母 todo「#831 後實跑重評，仍 Red 真拆」拆出。
