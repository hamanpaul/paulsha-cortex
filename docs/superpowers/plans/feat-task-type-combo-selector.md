---
status: accepted
work_item: feat-task-type-combo-selector
---

# feat-task-type-combo-selector Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_combo_selector.py`（selector 單元＋資料層；純函式測試不需 git fixture），先寫以下測試並確認全部失敗：
  - `test_fix_standard_combo_loads_and_passes_schema`：`load_combo(DEFAULT_COMBOS_DIR / "fix-standard.yaml", load_cards(DEFAULT_CARDS_PATH))` 成功，id 為 `fix-standard`、`task_type` 為 `fix`、gate_spine 恰兩項（verification／code-review）。
  - `test_fix_standard_manifest_passes_manager_spine`：`default_workflow_manifest("demo-work", change=None, combo_name="fix-standard")` 回傳 manifest 且 `validate_manager_spine()` 不拋錯。
  - `test_task_types_yaml_maps_fix_to_fix_standard`：`load_task_types(combos=...)` 後 `fix` 的 combo 為 `fix-standard`、`feat` 為 `feature-oneshot`、`docs`／`test`／`ci`／`refactor` 為 None。
  - `test_select_combo_fix_title_selects_fix_standard`：titles `{202: "fix(deck): ..."}` → `ComboSelection(combo_id="fix-standard", source="task-type-auto", task_type="fix")`。
  - `test_select_combo_feat_title_selects_feature_oneshot`：`feat: ...` 標題 → `feature-oneshot`／`task-type-auto`。
  - `test_select_combo_conflicting_matched_types_fail_closed`：兩 issue 標題分別 `feat: ...` 與 `fix: ...` → 拋 `ComboSelectionError`，訊息含兩個 issue 的分類明細。
  - `test_select_combo_unknown_type_fail_closed`：`perf(cli): ...` → fail-closed，診斷列出合法值域。
  - `test_select_combo_out_of_vocab_scope_fail_closed`：`fix(claimx): ...`（scope 不在受控詞典，taxonomy 判 `ambiguous`）→ fail-closed。
  - `test_select_combo_absent_title_bypass_with_marker`：無 prefix 標題 → `feature-oneshot`／`bypass-default`，reason 含 `absent`。
  - `test_select_combo_unparseable_title_bypass_with_marker`：`fix(: broken` → bypass，reason 含 `unparseable`。
  - `test_select_combo_combo_gap_type_bypass_with_marker`：`docs: ...`（matched 但映射 null）→ bypass，reason 含缺口 type。
  - `test_select_combo_no_titles_bypass`：titles 為 `None`（snapshot-drift）或空 dict → bypass，reason 分別為 `snapshot-drift`／`absent` 類。
  - `test_select_combo_override_wins_over_auto`：override=`fix-standard` 且標題 ambiguous → 仍成功，source 為 `explicit-override`。
  - `test_select_combo_override_unknown_combo_fail_closed`：override=`no-such-combo` → fail-closed。
  - `test_select_combo_deterministic`：同輸入呼叫兩次，dataclass 相等。
- [ ] 新增 `tests/test_combo_selector_wiring.py`（掛載與觀測面；fixture 比照 `tests/test_wiring_claim_time_sizing.py` 對 `start_canonical_workflow` 的既有測法），先紅：
  - `test_start_canonical_workflow_records_combo_selection`：帶 `fix` 標題訊號的 snapshot fixture claim 後，run 的 `combo == "fix-standard"` 且 `combo_selection` 的 source 為 `task-type-auto`。
  - `test_start_canonical_workflow_bypass_marker_visible`：無標題訊號 → run combo 維持 `feature-oneshot`、`combo_selection.source == "bypass-default"`。
  - `test_workflow_run_combo_selection_roundtrip`：`WorkflowRun.to_dict`／`from_dict` 保留 `combo_selection`；未帶欄位的舊 payload 載入為 None（回溯相容）。
  - `test_providers_projection_not_degraded_with_combo_selection`：registry state 帶 `combo_selection` 欄位後，`monitor/providers.py` 的 workflow 投影 status 為 ok、非 degraded。
  - `test_stat_combo_selections_aggregation`：`cortex stat --combo-selections` 輸出 `source × task_type` 計數 JSON（比照 `--retry-classifications` 測試模式）。
  - `test_contract_validates_start_combo_arg`：`validate_request` 對 start 帶非法 `combo`（空字串／非法字元）拋 `ValueError`，合法值通過。
- [ ] 驗收：`python3 -m pytest tests/test_combo_selector.py tests/test_combo_selector_wiring.py -q` 全部 FAIL（RED）。前置確認：`paulsha_cortex/deck/task_types.py` 已由 `design-task-type-taxonomy`（#139）落地；若尚未落地，先完成該票再回本票。

### 2. fix-standard combo 檔與 taxonomy 映射資料

- [ ] 新增 `paulsha_cortex/deck/data/combos/fix-standard.yaml`：`id: fix-standard`、`task_type: fix`；cards 依序 `workflow-claim`、`openspec-propose`、`writing-plans`、`worktree-isolation`、`tdd-red`、`subagent-build`、`verification`、`code-review`、`policy-commit`（comment 草稿七卡＋design D6 補回的 define/plan 兩卡）；gate_spine 維持草稿兩項：`after: verification` → `["reports/verify/*<task-slug>*.md"]`、`after: code-review` → `["reports/review/*<task-slug>*.md"]`；不設 band_triggered（adversarial-review 屬 #208 band 觸發，另案）。
- [ ] 修改 `paulsha_cortex/deck/data/task-types.yaml`：`fix` 的 `combo` 由 `null` 改為 `fix-standard`，其餘不動。
- [ ] 驗收：task 1 的三條資料層測試轉綠；`python3 -m pytest tests/test_deck_schema.py tests/test_deck_data.py tests/test_deck_task_types.py -q` 全綠（既有載入不受影響）。

### 3. selector 純函式模組

- [ ] 新增 `paulsha_cortex/deck/selector.py`：
  - frozen dataclass `ComboSelection`（`combo_id: str`／`source: str`（`task-type-auto`｜`explicit-override`｜`bypass-default`）／`task_type: str | None`／`reason: str`，reason ≤ 500 字元）。
  - `class ComboSelectionError(DeckSchemaError)`。
  - `select_combo(titles: Mapping[int, str | None] | None, *, taxonomy, override: str | None = None, default_combo: str = "feature-oneshot") -> ComboSelection`：override 非 None → 直接回 `explicit-override`（存在性驗證留給呼叫端 task 4 的 `load_combo`，此處只驗 id 格式 `[a-z0-9][a-z0-9-]*`）；否則逐 title 呼叫 `task_types.classify_title`，聚合規則照 spec R2（任一 `unknown_type`／`ambiguous` → 拋錯；matched 相異 type ≥2 → 拋錯；恰一 matched type → 查 `taxonomy` combo 映射，非 null 回 `task-type-auto`、null 回 `bypass-default`；零 matched → `bypass-default`）。錯誤訊息帶逐 issue `#N: <title> → <kind>/<reason>` 明細。
- [ ] 驗收：task 1 的 `test_select_combo_*` 全綠；模組無 I/O、無 import coordinator（單向依賴：coordinator → deck）。

### 4. claim 路徑掛載與 provenance 欄位

- [ ] `paulsha_cortex/coordinator/claim.py`：新增 `mapped_issue_titles(authority, *, snapshot_path=None) -> dict[int, str | None] | None`——`_load_snapshot`（line 206-259）取 payload 與 canonical hash；hash != `authority.snapshot_hash` → 回 None；否則自 `(repo, work_id)` canonical row 的 `sources` 收 `kind == "github_issue"` 的 `{issue_number: title}`（legacy row／無 sources → 空 dict）。
- [ ] `paulsha_cortex/coordinator/work_bridge.py`：`default_workflow_manifest`（line 144-157）加 `combo_name: str = "feature-oneshot"` 參數取代寫死字串；`start_canonical_workflow`（line 293）在建 manifest 前（line 334 前）執行 `select_combo(mapped_issue_titles(authority), taxonomy=load_task_types(combos=...), override=combo_override)`，新增 keyword 參數 `combo_override: str | None = None`；override 存在時先 `load_combo` 驗證存在（失敗即拋，fail-closed）。選擇結果餵給 `default_workflow_manifest(combo_name=...)` 與兩條 run 建立路徑的 `combo_selection`。
- [ ] `paulsha_cortex/coordinator/workflow.py`：`WorkflowRun` 加可選欄位 `combo_selection: dict[str, Any] | None = None`（緊鄰 `resolved_model_chain`，line 393 後；`__post_init__` 驗證鍵集合恰為 `{"source","task_type","combo","reason"}` 或 None）；`to_dict`／`from_dict` 走可選路徑。
- [ ] `paulsha_cortex/coordinator/registry.py`：`_manager_create_workflow_run`（line 1194）加 `combo_selection=None` 參數原樣寫入。
- [ ] `paulsha_cortex/coordinator/manager.py`：`apply_workflow_action` start 分支（line 7184-7208）自 args 轉錄 `combo_selection`；`apply_work_action`（line 7403-7435）比照 `extract_model_chain_override`（line 7416）取 `args.get("combo")` 傳入 starter 的 `combo_override`。
- [ ] `paulsha_cortex/monitor/providers.py`：`_WORKFLOW_V2_OPTIONAL_ROW_KEYS`（line 387-414）加 `"combo_selection"`（附 #202 註解，比照 line 399-412 的欄位註解慣例）。
- [ ] 驗收：task 1 wiring 測試中 `test_start_canonical_workflow_*`、`test_workflow_run_combo_selection_roundtrip`、`test_providers_projection_not_degraded_with_combo_selection` 轉綠；`python3 -m pytest tests/test_wiring_claim_time_sizing.py tests/test_work_bridge_source_owner_atomic.py -q` 全綠（既有 claim 行為不回歸）。

### 5. 明示 override 通道與控制契約

- [ ] `paulsha_cortex/coordinator/cli.py`：p_work（line 131-153）加 `p_work.add_argument("--combo", help="start 專用：明示指定 combo id，跳過 task_type 自動選牌（authoritative override）")`；request_args 組裝（line 274-318）加 `if args.combo is not None: request_args["combo"] = args.combo`。
- [ ] `paulsha_cortex/porcelain/run.py`：`_add_work_options`（line 46-72）加同語意 `--combo`，args 組裝點同步轉錄。
- [ ] `paulsha_cortex/control/contract.py`：`validate_request` work-action 分支（line 86-151）加：`action == "start"` 且 `combo` 存在時，必須為符合 `[a-z0-9][a-z0-9-]*` 的非空字串，否則拋 `ValueError("work-action start combo invalid")`。
- [ ] 驗收：task 1 的 `test_select_combo_override_*` 與 `test_contract_validates_start_combo_arg` 轉綠；`python3 -m pytest tests/test_control_contract.py tests/test_control_client.py -q` 全綠。

### 6. stat 可觀測面與 CLI help 同步（R-16）

- [ ] `paulsha_cortex/coordinator/cli.py`：p_stat（line 69-86）加 `--combo-selections` flag；stat 分支（line 368-384）比照 `--retry-classifications` 實作——掃 `reg.list_workflow_runs()`，以 `(combo_selection.source, combo_selection.task_type)` 計數（欄位為 None 的 run 計入 `unrecorded`），輸出 `{"combo_selections": {...}}` JSON；缺 flag 時錯誤訊息（line 387）同步補列新 flag。
- [ ] `paulsha_cortex/cli.py`：`_HELP` 的 `jobs, stat` 行與 `_WORK_HELP`（約 line 50-70）的 `start` 說明補 `--combo` override 與 stat 彙總語意一句。
- [ ] 驗收：task 1 的 `test_stat_combo_selections_aggregation` 轉綠；`python3 -m pytest tests/test_cli_help_alignment.py tests/test_cli_stat_decomposition_depths_223.py tests/test_coordinator_cli_flags.py -q` 全綠；`cortex work --help` 與 `cortex stat --help` 輸出含新 flag。

### 7. operator 文件

- [ ] `docs/unified-work-lifecycle.md`：在 claim／workflow 建立敘述段補「combo 自動選擇」小節：訊號來源（durable snapshot 的 issue 標題、#139 taxonomy）、feat/fix 映射現況與缺口 type 的 bypass 語意、fail-closed 情境與解法（修標題或 `--combo`）、`--combo` override 的 authoritative 語意、`cortex stat --combo-selections` 查詢方式；並明載 fix-standard 相對 comment 草稿補回 define/plan 卡的原因（`validate_manager_spine` 全 phase spine）。
- [ ] 驗收：段落存在且與 spec R1-R6 一致；`python3 -m policy_check` 的 doc 相關規則無新增 FAIL。

### 8. 交付要件

- [ ] `changelog.d/feat-task-type-combo-selector.md` fragment 已新增且已 commit（R-09 硬性 gate，只 add 不 commit 仍 FAIL）。
- [ ] `CHANGELOG.md [Unreleased]` 對應 entry（Refs #202）。
- [ ] cortex CLI help 同步（R-16；task 6 的 `--combo`／`--combo-selections` 文字即為其落點，交付前重驗）。
- [ ] 帶 PR 上下文執行 policy_check 確認 0 fail：`python3 -m policy_check --repo . --pr-title "..." --pr-body "..." --pr-labels "..." --pr-base-ref main --pr-head-ref "feature/202-feat-task-type-combo-selector"`。
- [ ] `python3 -m pytest tests/ -q` 全綠。
