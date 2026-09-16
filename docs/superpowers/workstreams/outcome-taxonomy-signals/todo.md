---
status: accepted
work_item: outcome-taxonomy-signals
---

# Outcome taxonomy 與 launch failure 訊號保真

## Boundary

- Issue：`hamanpaul/paulsha-cortex#826`。
- 範圍包含共享 `outcome_taxonomy.py`／`provider_outcome.py`、三條 launch failure
  寫入路徑（dispatcher／autonomy／workflow manager），及 slice gate_reason／workflow
  needs_human 消費端。新增分類必須完整穿過 persisted job 與 runtime diagnostic。
- 不新增 identity reasoning_effort 設定、不移除 launcher effort 預設、不實作
  durable backoff（#825）；不以模型散文／nested tool results 當成 provider authority。

## Tasks

- [ ] 新增 `TextSignal.EFFORT_NOT_SUPPORTED`／`EXECUTABLE_NOT_FOUND` 與
      `StructuredKind.EXECUTABLE_NOT_FOUND`／`LAUNCH_FAILED`，映射至
      `OutcomeFamily.ENVIRONMENT`；effort regex 擷取 effort、model，能辨識
      `Reasoning effort "xhigh" is not supported for model "mai-code-1-flash-picker"`。
- [ ] executable 訊號必須有啟動程式識別／shell command-not-found 上下文，或來自
      可證實啟動失敗的 exit 127／typed exception。裸 `not found`／HTTP 404／
      model not found／一般工作檔案 `No such file or directory` 不得直接分類為
      executable_not_found 或觸發 reroute；provider text 與 model text 保持分層。
- [ ] 保留 structured terminal／controller interruption 優先序，再處理可信 exit 127，
      然後 text 的 rate limit→quota→auth→effort→executable→content→transient。
      `classify_text(exit_code=127)` 的單元契約與 provider 分類的 structured authority
      各自測試，不能讓一般 exit 127 覆蓋已存在的強 authority 終局證據。
- [ ] 新增 `ProviderOutcome.EFFORT_NOT_SUPPORTED`／`EXECUTABLE_NOT_FOUND`／
      `LAUNCH_FAILED` 與對照；加入非持久的 `reroutable` property，僅前兩者且
      authority 非 hint 時 True。`to_dict()` 保持 outcome／authority／reason／retryable
      加可選 reset_at；`launch_failed` 本身不自動 retry／reroute。
- [ ] `JobRegistry.update_headless_result` 加 optional executor／model_id（僅非 None
      才覆寫），不另造 registry enum validation：現行只驗 payload shape。
      以三種 outcome 寫入→reload→`classification_from_job` round-trip 驗證相容。
- [ ] `Dispatcher.poll_headless_done` 遇 pid／log_path 缺失時記 structured
      launch_failed，reason 精確寫 `launch handle missing: pid=None, log_path=None`。
      這可能是 Manager 在 create_job 與 attach_handle 間中斷；不可偽稱 launcher
      已拋例外。更新既有 missing-launch-handle 測試，保留真實缺失欄位。
- [ ] `autonomy._fail_launching_job` 接收 executor／model_id／exc，保存身分、
      provider_outcome 與 `runtime_diagnostic`（reason=`launch-failed`、exception
      type／detail、source=`autonomy.dispatch_ready:launch`、job_id）；workflow
      `_dispatch_workflow_card` 的 launch exception 同樣保存 diagnostic，source
      明確為該函式。可驗證的缺 executable exception 才使用相應細分類；無法確定的
      `FileNotFoundError`（例如 cwd 缺失）保留 launch_failed 原因，不猜測 executor 壞掉。
- [ ] `_poll_workflow_job` 依 runtime diagnostic 的 reason 分流：真正
      runtime-contract failure／sandbox drift 繼續禁止 provider reroute；
      launch-failed 診斷不因「任何 dict 都算 contract failure」而清掉原 classification。
      保留原始 exception／缺 handle reason，不用 generic runtime-contract-failed 覆蓋。
- [ ] reroute 條件採 `retryable or reroutable`，仍受 MAX_PROVIDER_RETRIES、品質資格、
      明確 pin、工具權限及 independence-domain 限制；不可換同一失敗配置無限重跑。
      needs_human diagnostic 保留 executor／model／可擷取的 effort；缺欄位明示未知。
- [ ] 單元測試涵蓋 effort 正例、明確 command-not-found／127 空輸出、一般檔案／
      model／HTTP404 負例、model-text-only 負例、structured 429／interruption 優先；
      保留 `test_no_signal_classifies_as_unknown_hint_and_not_retryable`。
- [ ] fake launcher 觸發真 exception→寫 registry→reload→workflow poll，驗證
      launch_failed／來源原因仍在、真正 runtime-contract 不 reroute；另一個合格且
      獨立候選存在時，effort_not_supported 可有界 reroute。不能只 seed 已分類 row
      就宣稱 producer→consumer 接線已驗證。
- [ ] 新增 `tests/test_outcome_taxonomy_signals_826.py`，以 copilot effort 日誌、
      exit-127 空日誌與缺 handle 三種 row 經 `poll_headless_done`，斷言 outcome
      authority 非 hint，slice gate_reason 分別包含 builder-failed 與具體分類。
      覆蓋三條寫入路徑與 workflow 消費端，保留既有 retry／contract fail-closed gates。
- [ ] 更新 outcome 詞彙文件、changelog fragment 與 `CHANGELOG.md [Unreleased]`；
      透過 Cortex 記錄 RED／GREEN、完整 gates、獨立 review、merge 與 runtime 診斷證據。
