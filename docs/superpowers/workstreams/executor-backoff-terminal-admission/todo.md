---
status: accepted
work_item: executor-backoff-terminal-admission
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Executor backoff terminal 記錄、reset hint 解析與 workflow lane admission（child C＋D＋parser）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#928`。Parent：`executor-durable-backoff`／#825（母 [spec](../../specs/executor-durable-backoff-spec.md)、
  [design](../../specs/executor-durable-backoff-design.md)、[todo](../executor-durable-backoff/todo.md)），本票承接母 spec
  **R4 parser 部分、R5、R6、R3 消費端、R2 尾段對帳 seam**；母 design D3（parser authority 不升級）、D4 前半（workflow admission）逐條生效。
- 兄弟：#850 `executor-backoff-store-core`（母 design A：store 本體，**必須先 merge**）；#929
  `executor-backoff-slice-consumers`（E＋F：slice lane 與 request／tick consumers，在本票之後）。
- 觸及模組（3 個 production 模組 → `domain_breadth: 1`）：新增 `paulsha_cortex/coordinator/reset_hint.py`（或併入
  `outcome_taxonomy`）、`provider_outcome.py`（`classify_provider_failure` 填 `reset_at`）、`manager.py`
  （`record_executor_backoff_from_job` 的兩個終局呼叫點、`_executor_backoff_filter`、`_dispatch_workflow_card`、
  `_runtime_preflight_gate`、`_provider_failure_reroute`）。`registry.py` 只在 job 未知欄位被 fail-closed 拒絕時放行
  `dispatch_reroute`。`state_consistency: 1`：本票不新增 durable 狀態，跨 process 一致性由 #850 的 store 負責；本票只須
  在單一 admission 內正確消費 store 的 active／expired／unknown 三態並把 unknown 傳遞出去。
- 只呼叫 `executor_backoff.py` 的公開 API（`record_backoff`／`active_backoff`／reconciliation 入口），不改其語意、不動
  GitHub `provider_backoff.py`；不改 slice lane 的 `autonomy.dispatch_ready`、`launcher`、`manager_daemon` request 結果與
  `run_tick`／`apply_slice_action` 的 `dispatch_skipped_by_backoff`（#929）。不新增 override／clear CLI、不改 `spawn_admission`、
  不改 `_workflow_identity_candidates` 候選順序、不放寬 pin／independence、不做 quota pool／forecast／reservation（R9）。
- 沿用已落地契約：#830 `classify_dispatch_result` 的 `decision` 分類——`reason == "executor-backoff"`／`"executor-backoff-unknown"`
  的決策在 `resume_workflow_run` 兩個 retry 分支已「原樣回傳、不計 retry、不造 job_id」，**不需另加特判**，只需測試釘住。

## 現場證據

- 母票 #825 現象：codex spark 同一用量上限連燒三次（終局 `try again at Sep 7th 12:23 PM`）、claude `rate_limit_event … reset_at`
  已由 #499 帶回 job 但無消費者、copilot `quota` 分類後下一 tick 仍派同一 identity；分類全對，錯在結果不驅動跨 tick 退避。
- 現行 main：`manager._provider_failure_reroute` 是 in-memory、單卡有界（`terminal_contract.MAX_PROVIDER_RETRIES = 2`）、不讀
  `classification.reset_at`；`outcome_taxonomy._RESET_AT_KEYS` 只解析結構化 reset；`provider_outcome.ProviderFailureClassification.reset_at`
  是既有可選欄（`_PROVIDER_OUTCOME_OPTIONAL_FIELDS`）；`_dispatch_workflow_card` 無 gate 時直接 `_select_workflow_identity` 取 `[0]`。
- 2026-09-17 實算：母票三件套 sizing 8／Red（三種 combo 同分），依母 todo 第一條「#831 後實跑重評，仍 Red 真拆」拆出本票。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_reset_hint_parsing.py` 與 `tests/test_executor_backoff_workflow_lane.py`（沿
      `tests/test_provider_failure_recovery.py` 的 `_two_builder_identities`／`_seed_builder_job`／`_ResumeDispatcher` 樣板），
      斷言逐條對應 #928「驗收」節；現行必須 RED（`parse_reset_hint` 不存在／第一候選 rate_limited 後下一次 resume 仍派同一 identity）。
- [ ] **T2 source／reset hint（R4 parser、D3）**：`parse_reset_hint(text, *, now, tz=None) -> int | None`：Retry-After 非負秒
      （0 → now，margin 由 store 加）、Codex 月日／AM-PM 只依明示 timezone 與基準年解析，該年已過 → None、不滾到明年；負值／非有限／
      無訊號／模糊 DST → None；timezone／now seam 可注入 deterministic fixture。接進 `classify_provider_failure`：文字命中
      `RATE_LIMIT`／`QUOTA` 時填既有 `reset_at`，結構化 `resetsAt` 優先，不改 `authority`、不新增 vocabulary、`to_dict()` 鍵集合不變。
- [ ] **T3 source／終局寫入（R5）**：`record_executor_backoff_from_job(coordinator_root, job, classification) -> ExecutorBackoff | None`，
      slice lane 終局分類分支（`gate_reason = f"builder-failed-{outcome}"` 處，`coordinator_root` 由 `registry._state_path` 導出）與
      workflow lane `resume_workflow_run` job-failed 分支（含 runtime_diagnostic 歸零分支）共用；只接受 `rate_limited`／`quota` 且
      `authority != hint`；用終局 job 的真 `executor`／`model_id`／`job_id` 與 immutable terminal time（`exited_at`）送 store；
      缺欄位／`coordinator_root is None`／outcome 不符 → 回 None、不造 key、不炸終局收斂、留診斷。
- [ ] **T4 source／對帳 seam（R2 尾段、R3 消費端）**：admission 前以 registry 既有持久 job 終局作 caller inventory 餵 store 的
      reconciliation；store 回 unknown（corrupt／unreadable／未 ack pending intent）→ admission 回 unknown 決策，不當空檔、不捏造
      deadline、不耗 provider retry；有效 store 恢復後可重新 admission。不新增 registry writer、不掃 live state 以外的來源。
- [ ] **T5 source／workflow admission（R6、D4 前半）**：`_executor_backoff_filter(candidates, *, coordinator_root, now) ->
      tuple[list, list[dict]]`（`skipped` 元素 `{"executor","model_id","retry_after_epoch"}`，順序不變、不放寬 domain）；
      `_dispatch_workflow_card` 在 `_runtime_preflight_gate` 與 `_select_workflow_identity` 前套用（`_runtime_preflight_gate` 新增
      keyword `candidates=None`，lookup 對 cooldown identity 投影 `ProviderFreshness(status="degraded", source="executor-backoff")`）；
      `_provider_failure_reroute` 新增 keyword `coordinator_root` 先套同一過濾、`eligible` 空回 None；全候選 cooldown → 回
      `{"run_id","current_phase","reason":"executor-backoff","retry_after_epoch":<min deadline>,"skipped":[...]}`（含 #205 override
      單元素命中），不落 needs_human、不建 worktree／job；候選中有 unknown → `reason: "executor-backoff-unknown"` 附診斷，不把
      已知最早期限當全體可恢復時間；`resume_workflow_run` 沿 #830 decision 契約原樣回傳、`attempts` 不變（測試釘住）。
- [ ] **T6 source／證據**：過濾後派出的 job 帶 `dispatch_reroute = {"source": "executor-backoff", "skipped": [...]}`（registry
      fail-closed 拒絕未知欄位時同步放行）；每次 skip `logger.info` 一行（executor／model_id／retry_after_epoch 或 unknown 原因）。
- [ ] **T7 tests／回歸**：`tests/test_provider_backoff.py`、`tests/test_provider_failure_recovery.py`、`tests/test_provider_outcome.py`、
      `tests/test_outcome_taxonomy.py`、`tests/test_dispatch_runtime_preflight.py`、`tests/test_dispatch_decision_contract.py`、
      `tests/test_executor_backoff.py`（#850）全綠、不改斷言；補「同終局 job 重送不增 hits」「store unknown → 可區分 reason、無假
      deadline」「到期後可再派回第一候選」斷言。
- [ ] **T8 documentation**：`docs/unified-work-lifecycle.md` 派工段補「executor×model cooldown 於 preflight／reroute 前過濾、
      `executor-backoff`／`executor-backoff-unknown` 決策不落 needs_human、`dispatch_reroute` 收據」；新增
      `changelog.d/executor-backoff-terminal-admission.md` 並同步 `CHANGELOG.md [Unreleased]`；明寫 slice lane 尚由 #929 承接、
      #825 母票不因本票 merge 關閉。
