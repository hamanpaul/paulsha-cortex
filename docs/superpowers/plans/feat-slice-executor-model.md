---
status: accepted
work_item: feat-slice-executor-model
---

# feat-slice-executor-model Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_slice_executor_model.py`（frontmatter／dispatch fixture 比照 `tests/test_persona_phase4_fanout_autonomy.py` 的 `_write_spec`／`_meta`／`_FakeDispatcher`／`_RecordingLauncher`；daemon request 測試比照 `tests/test_coordinator_manager_daemon.py` 以 `build_request_executor(dispatch_ready_fn=..., scan_specs_fn=..., workflow_identity_registry=...)` 注入 fake），先寫以下測試並確認全部失敗：
  - `test_frontmatter_paired_executor_model_id_parsed`：spec frontmatter 宣告 `executor: codex`＋`model_id: gpt-5.4-codex`，parse meta 帶兩值、`parse_error` 為 None，其餘欄位不變。
  - `test_frontmatter_executor_without_model_id_invalid`：只宣告 `executor` → `parse_error.code == "invalid-frontmatter"` 且 field 為 `model_id`；對稱測只宣告 `model_id` → field 為 `executor`。
  - `test_emitted_frontmatter_fields_include_identity`：`paulsha_cortex.deck.schema.EMITTED_FRONTMATTER_FIELDS` 含 `executor` 與 `model_id`（與 `tests/test_deck_contract_alignment.py` 的雙向等式互鎖）。
  - `test_dispatch_ready_per_slice_override_reaches_job_row`：兩個 ready slice、僅其一宣告 override；`launcher_factory` 只對宣告者被呼叫且收到 registry 中該 `ModelIdentity`；該 slice job row 經 `attach_launch_handle` 後 `executor`/`model_id` 等於宣告值；未宣告者用預設 launcher 派出。
  - `test_dispatch_ready_unknown_identity_fail_closed_lists_available`：宣告 registry 沒有的 identity → `DispatchReadyError`，錯誤訊息含可用 `executor/model_id` 清單；該 slice 標 `needs_human`、無 launch 呼叫；同批另一 slice 照常派出（`exc.jobs` 含之）。
  - `test_dispatch_ready_no_declaration_behavior_unchanged`：無任何宣告時 `launcher_factory` 未被呼叫、預設 launcher 原樣使用（含 builder persona 的 `as_commit_required` 轉換），與現行為位元一致。
  - `test_held_reasons_classified`：dep 在本批未完成 → `deps-unsatisfied:<id>`；批外且 handoff 目錄有 `<id>.json` → `deps-external:<id>`；批外且無 manifest → `deps-unknown:<id>`。
  - `test_ready_cli_unknown_dep_stderr_diagnostic`：`cortex ready --specs-dir`（`coordinator/cli.py` main 注入 `is_satisfied`）stdout JSON 與 exit code 不變，stderr 出現含 `deps-unknown:<id>` 的顯式診斷行。
  - `test_fanout_request_unknown_builder_identity_rejected`：fanout request 帶 registry 沒有的明確 `(executor, model)` 對 → request 失敗、錯誤列可用清單、`dispatch_ready_fn` 未被呼叫。
  - `test_fanout_request_without_model_unchanged`：不帶 model 的 fanout request 照現行派工，不觸 registry 驗證。
- [ ] 驗收：`python3 -m pytest tests/test_slice_executor_model.py -q` 顯示上述測試全部 FAIL（RED）。

### 2. frontmatter 契約擴充

- [ ] `paulsha_cortex/coordinator/autonomy.py`：`_normalize_frontmatter`（line 114-127）的 `allowed` set 加入 `executor`/`model_id`；新增成對與非空字串驗證——單獨宣告其一 raise `verification.ContractValidationError(<缺漏欄名>, ...)`；`parse_spec_frontmatter` 的 meta 預設（line 72-81）與 `_normalize_frontmatter` 回傳 meta 補 `"executor": None, "model_id": None`（parse_error 回退分支同步帶值）。
- [ ] `paulsha_cortex/deck/schema.py`：`EMITTED_FRONTMATTER_FIELDS`（line 12-20）加入 `"executor"`、`"model_id"`；`paulsha_cortex/deck/compile.py` 的 `_render_frontmatter`（line 287）不改（不輸出新欄）。
- [ ] 驗收：task 1 前三條測試轉綠；`python3 -m pytest tests/test_deck_contract_alignment.py tests/test_persona_phase4_fanout_autonomy.py tests/test_fix_deck_emit_frontmatter.py -q` 全綠。

### 3. dispatch_ready per-slice identity 驗證與 launcher 覆寫

- [ ] `paulsha_cortex/coordinator/autonomy.py`：`dispatch_ready`（line 404）簽名新增 optional `identity_registry=None`／`launcher_factory=None`。per-slice try block（line 446 起）開頭解析 identity：meta 兩欄皆非空時——`identity_registry` 為 None 則 lazy `load_model_identities()`；`registry.get(executor, model_id)` 回 None → raise `ValueError`，訊息含宣告值與可用 `executor/model_id` 清單（形狀比照 `manager.py:5278-5283`）；通過則以 `launcher_factory(identity)` 建 per-slice launcher（factory 為 None 時 raise，避免靜默用錯 identity），builder persona 對 per-slice launcher 套 `as_commit_required`（與 line 439-442 預設路徑同語意）。未宣告 → 沿用預設 launcher，路徑零改動。錯誤走既有 per-slice except（`_fail_launching_job`／`_mark_slice_needs_human`／收進 `DispatchReadyError`）。
- [ ] 驗收：task 1 的三條 `dispatch_ready` 測試全綠；`python3 -m pytest tests/test_persona_phase4_fanout_autonomy.py -q` 全綠（預設路徑不回歸）。

### 4. daemon 接線與 request 層 identity 驗證

- [ ] `paulsha_cortex/coordinator/manager.py`：`run_tick`（line 1841）簽名補 optional `identity_registry`／`launcher_factory`，透傳給 `autonomy.dispatch_ready`（line 1890-1898 呼叫點）。
- [ ] `paulsha_cortex/coordinator/manager_daemon.py`：`build_request_executor` 的 dispatch 分支（line 671-679）、fanout 分支（line 706-715）與 tick kwargs（line 723-732），以及 `build_periodic_tick_runner`（line 865-874），傳入 `identity_registry=(workflow_identity_registry or load_model_identities())` 與 `launcher_factory=lambda identity: _resolve_launcher(identity.executor, launcher, allow_unsafe=<該路徑現值>, model=identity.model_id)`（比照 line 417-422 既有 lambda 形狀）。
- [ ] 同檔 R5：fanout/tick/dispatch request 在 executor 與 model（含 default 帶入後）皆為明確字串時，先以 registry `get` 驗證，查無 → raise `ValueError` 帶可用清單（request 回 error）；`build_periodic_tick_runner` 對 `default_executor`＋`default_model` 明確對同樣驗證，失敗記入 tick error、本輪不派工；model 為 None → 不驗證。
- [ ] 驗收：task 1 的兩條 request 測試全綠；`python3 -m pytest tests/test_coordinator_manager_daemon.py tests/test_coordinator_cli_tick.py tests/test_manager_daemon_tick_isolation.py tests/test_manager_daemon_tick_backoff.py -q` 全綠。

### 5. depends_on 批外診斷

- [ ] `paulsha_cortex/coordinator/autonomy.py`：新增 pure helper `classify_batch_dependency(dep, *, batch_ids, handoff_dir) -> str | None`——dep 在 `batch_ids` → None（沿用既有 unsatisfied 判定）；批外且 `Path(handoff_dir) / f"{dep}.json"` 存在（比照 `completion.py:577` 的定位方式）→ `"deps-external:<dep>"`；批外且無 manifest → `"deps-unknown:<dep>"`。`detect_cycles`（line 316）docstring 補「批外邊不算環、診斷責任在 classify helper」。
- [ ] `paulsha_cortex/coordinator/manager_daemon.py`：`_held_reasons`（line 232-243）簽名帶入 `batch_ids` 與 `handoff_dir`，批外 dep 改用 classify helper 產出 `deps-external`/`deps-unknown`，in-batch 未滿足維持 `deps-unsatisfied:<dep>`；`build_runtime_status_provider` 的呼叫點（line 329）同步傳參。
- [ ] `paulsha_cortex/coordinator/cli.py`：`ready` 分支（line 238-247）計算各 meta 的批外診斷，對含 `deps-unknown` 的 slice 於 stderr 印一行診斷（stdout JSON 與 exit code 不變）。
- [ ] 驗收：task 1 的 `test_held_reasons_classified` 與 `test_ready_cli_unknown_dep_stderr_diagnostic` 全綠；`python3 -m pytest tests/test_coordinator_manager_daemon.py tests/test_coordinator_cli_flags.py -q` 全綠。

### 6. CLI help 與 docs 同步（R-16／R-18）

- [ ] `paulsha_cortex/coordinator/cli.py`：fanout／tick 的 `--model` help（line 105、183）補「spec frontmatter 宣告 executor/model_id 時逐 slice 覆寫本值；明確指定的 (executor, model) 須為 model-identities 已註冊身分」；`paulsha_cortex/porcelain/run.py` 的 `--model` help（line 39）同步。
- [ ] `README.md`：fanout／tick 段（line 186-193、279-284 附近）補 per-slice 覆寫與 registry 驗證一段；`model-identities.yaml` 段（line 458 附近）補「fanout/tick 明確 model 與 per-slice 宣告皆查此 registry」。
- [ ] `docs/superpowers/specs/fix-deck-emit-frontmatter-dispatch-contract.md`（README:255 指定的 auto dispatch 契約文件）：frontmatter 欄位清單補 optional `executor`/`model_id`（成對、registry 驗證、未宣告沿用 fanout 預設）。
- [ ] 驗收：`cortex fanout --help`／`cortex tick --help`／`cortex run fanout --help` 輸出含新說明；`python3 -m pytest tests/test_cli_help_alignment.py tests/test_porcelain_run.py -q` 全綠。

### 7. 交付要件

- [ ] `changelog.d/feat-slice-executor-model.md` fragment 已新增且已 commit（R-09 硬性 gate，只 add 不 commit 仍 FAIL）。
- [ ] `CHANGELOG.md [Unreleased]` 對應 entry（Refs #294；標注 R5 的 fanout/tick 明確 model 需註冊之行為變更）。
- [ ] cortex CLI help 同步（R-16；task 6 的 help 文字即為其落點，交付前重驗）。
- [ ] 帶 PR 上下文執行 policy_check 確認 0 fail：`python3 -m policy_check --repo . --pr-title "..." --pr-body "..." --pr-labels "..." --pr-base-ref main --pr-head-ref "feature/294-feat-slice-executor-model"`。
- [ ] `python3 -m pytest tests/ -q` 全綠。
