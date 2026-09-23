---
status: accepted
work_item: reviewer-honest-stop-terminal
---

# verify／review 卡誠實非通過 terminal 走明示停止落地設計

## Decisions

### D1 另立 predicate，只餵落地分支，不擴 `_retryable_nonpassing_workflow_terminal`

`_retryable_nonpassing_workflow_terminal` 回 `True` 在四處被消費：#717 落地分支（11987）、`_discard_failed_planner_sandbox`（6821）、`_dispatch_workflow_card.retryable_latest`（10585）、`resume_workflow_run` 的 retry 分支（11817）。後兩處把 `True` 當成「舊 job 可棄、改派新 job」的 retry authority。直接把 verify／review 塞進它的 phase 集合，operator 顯式 `resume` 對 reviewer 卡的語意會跟著從「重讀同一 job」變成「重派」，還得連帶處理 reviewer sandbox 的重派回收（#569 刻意只在 forced 路徑回收）。這超出本票範圍。

因此新增 `_explicit_stop_gate_terminal(job) -> dict[str, object] | None`，**只**在 `resume_workflow_run` 的新落地分支消費；四個既有消費點與其測試逐字不變。reviewer 卡的重派出口維持既有的 `retry-card`（force_new_card，#569 先回收 sandbox 並驗 drift，可帶 #752 `--reason` 裁決）與 `retry-build`（#750 讀 verify 非通過 terminal 當 repair 回饋）。predicate 回傳已解析的 envelope（不是布林），讓落地分支不必再讀第二次 log，也不必另寫 `_declared_card_terminal_status` 那類 helper。

### D2 形狀判準＝既有「通過形狀」減去 status，再加 fail-closed 型別收斂

- 共同的 job 條件照抄 #717 predicate 的前五項，只把 phase 換成 verify／review；log 解析沿用 `_extract_terminal_json`（`ValueError` → `None`），不另寫解析器。
- verify 的 key set 與 `terminalize_workflow_job` verify 分支的 `required` 相同。`summary` 非空字串、`details` 為 dict 或非空字串，與該分支對通過形狀的要求一致（字串 details 的正規化同源）。
- `reports`（verify、review 共用）：型別收斂到「list，每個元素是鍵集恰為 `{"path", "body"}`、兩值皆為 strip 後非空字串的 dict」，與 `launcher._claude_review_json_schema` 的 report item schema（claude／agy 工具層的形狀）相同，`[42]` 這類元素因此回 `None`。刻意不套用的有兩處：
  - 允許 `reports == []`。非通過狀態不發佈 report，病因敘事在 `summary`／`details`／`reason`／`findings`；#261 R1 自己的契約測試（`tests/test_terminal_result_contract.py` 的 non-passing verification fixture）就是 `reports: []`。若拒收空 list，一個沒寫 report 的誠實停止會掉回 terminalize 例外，也就是本票要修的症狀。
  - 不跑 `_inline_terminal_reports` 的 manifest（`declared_outputs` 比對）、governed root（`reports/verify|review/*.md`）與大小上限。那三項守的是「發佈到 repo 的 report 不越界」，非通過分支不發佈任何 report（R5），套用它只會讓路徑寫錯的誠實停止掉回例外，沒有防到任何寫入。
- review 的 key set 是 `terminalize_workflow_job` review 分支的 `required`，加上此處必備的 `status`，外加可選的 `authority_hashes`。`findings` 收斂到「list，每個元素是 dict」。`authority_hashes` 的內容、`findings` 各元素的 finding schema 都不驗：非通過狀態下兩者都不進任何採信路徑，只當敘事帶給 operator（agy 的 `authority_hashes` 是 `[{key, value}]` 陣列，不驗內容也就不需要 `_fold_agy_key_value_map`）。
- 型別錯（`schema_version` 為 `True`、`findings` 非 list 或含非 dict 元素、`reports` 含非 `{path, body}` 元素、`reason` 空白…）、多出未知鍵、phase／kind 錯配，一律回 `None`，交回既有 terminalize 路徑 fail closed。相對通過形狀，放寬的只有三點：非通過狀態不再被當例外；`reports` 允許空 list；report 的 manifest／root／大小檢查不套用。後兩點都只因為非通過分支不發佈 report。其餘形狀契約沒有放寬。

### D3 病因投影重用 `_model_terminal_diagnostics`，不另立截斷規則

新增 helper（建議名 `_gate_terminal_stop_diagnostics(job, raw) -> terminal_contract.TerminalDiagnostics`）。它依 phase 組出一個有序 mapping：

- verify：`{"summary": ..., **{f"details.{k}": v for k, v in details.items()}}`，details 是字串時為 `{"summary": ..., "details": ...}`。
- review：`{"reason": ..., **{f"findings[{i}]": f for i, f in enumerate(findings)}}`。

這個 mapping 以 `{"diagnostics": mapping}` 丟給既有的 `_model_terminal_diagnostics()`，全體 2000 字預算、key 截 64 字、非字串值轉 canonical JSON、`…` 截斷標記都沿用同一份實作。`TerminalDiagnostics` 的欄位：

- `job_id`
- `observed_head`：job 的 `subject_head`，缺值時取 `dispatch_head`，與 `_terminal_parse_diagnostics` 同一取法。
- `reason`：`f"workflow {label} terminal reported non-passing status: {status}"`，與 terminalize 原句逐字相同，operator 既有的 grep 仍命中。
- `validation_path`：`"$.status"`。
- `model_diagnostics`：上面組出的 rows。

attention 的 `detail` 用 `model_diagnostics_text()` 組字，400 字截斷交給 `DiagnosticReason.__post_init__`。

### D4 落地分支的位置與順序

放在 `resume_workflow_run` 內 #717 `if _retryable_nonpassing_workflow_terminal(job):` 分支**之後**、`_malformed_workflow_card_terminal` **之前**（兩者都只認 plan／build，與新分支互斥，順序不影響語意，只是讓三條明示停止／schema 分支相鄰）。分支內依序：

1. `raw = _explicit_stop_gate_terminal(job)`，`None` 就落到既有路徑。
2. 組 D3 的 diagnostics 與 `declared_status = raw["status"]`。
3. `_discard_reviewer_sandbox(job, coordinator_root=coordinator_root, require_candidate_unchanged=True)`。擲 `ValueError` → 走 D5 的 drift 出口。
4. `current = registry.get_workflow_run(run.run_id)`，以 `_manager_update_workflow_run(run.run_id, facets=tuple(dict.fromkeys((*current.facets, "needs_human"))), gate_status="running", needs_human_reason=diagnostic_reason(...))` 寫入。
5. 回傳 spec R3 的 dict。

不呼叫 `terminalize_workflow_job`、`_WorkflowReportPublicationTransaction`、`_run_gate_execution_identity`、`_assert_terminal_gate_consistency`，與 #717 分支相同：非通過狀態不採信任何東西，也就不需要採信前檢查。

### D5 reason 詞彙、source 與 drift 出口

- reason code 依 phase 拆成 `verification-terminal-explicit-stop`／`review-terminal-explicit-stop`，與 terminalize 例外字串的 `verification`／`review` 對齊，符合 `DIAGNOSTIC_REASON_CODE_RE`。不重用 `card-terminal-explicit-stop`，讓 operator 一眼分得出是 builder 卡還是 gate 卡在要人。
- source 沿用 `manager._poll_workflow_job:explicit-stop`，與 #717 同一個出口標記。
- `evidence_refs=(job["log_path"],)`（非空字串時），context 帶 `run_id`／`work_id`／`job_id`／`card`／`declared_status`／`phase`（共 6 鍵，低於 16 鍵上限）。
- sandbox 回收擲 `ValueError` 時，沿用 11853 既有詞彙 `reviewer-candidate-drift`，detail 為 `f"{label} terminal 明示要求停止（status={declared_status}），但 reviewer sandbox 回收失敗：{summarize_exception(exc)}"`，同一 source、同樣帶 evidence_refs，回傳 `reason: "reviewer-candidate-drift"`，不 raise。現行 terminalize 失敗路徑在 except 裡回收 sandbox，drift 時例外直接往上擲、連 needs_human 理由都沒寫；新分支把它收斂成可稽核的停止。已知且接受的殘餘：`_discard_reviewer_sandbox` 只在 sandbox 還在時比對 candidate 樹，所以 drift 之後的 operator resume 會改落一般明示停止。這與現行 terminalize 失敗路徑、non-zero exit drift 分支在 resume 之後的行為相同，本票不另加重驗。
- `gate_status` 維持 `"running"`，與 #717 分支和現行 terminalize 失敗路徑相同，`retry-build`（ongoing `needs_human` verify／review run）與 `retry-card` 的受理條件因此不受影響。

### D6 operator 顯式 resume 的語意

`resume_workflow_run(operator_resume=True)` 會先剝 `needs_human` 並設 `retry_failed=True`。由於 D1 沒動 retry 判準，11817 分支對該 job 不成立，poll 再撿到同一個 job、再落同一個明示停止。結果冪等：同一個 reason、`recorded_at` 更新、不派 job、不 raise；sandbox 已回收，第二次 `_discard_reviewer_sandbox` 因目錄不存在直接 return。`next_actions` 由既有 `needs_human_next_actions`＋`_phase_recovery_actions` 導出：`abandon` 永遠在，另外因為卡 job 已 terminal 且無 evidence，所以有 `retry-card`。

### D7 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| verify needs_human（#874 本文引用的 summary／details），agy 與 claude 各一 | 參照 `tests/test_review_authority_hashes_echo_922.py::_review_terminalize_fixture` 另建 verify phase run＋`verification` reviewer job（`subject_head == candidate_head`）。agy 份：agy identity，log 為 `{"response": ...}` 單行 envelope，response 是「fenced 文字＋最後一行帶 `toolAction`／`toolSummary` 的 structured output」（#888 實機形狀）。claude 份：claude identity，log 為 `{"type": "result", "result": ...}`。兩份都走 `resume_workflow_run(operator_resume=False)` | 不 raise；`reason == "verification-terminal-explicit-stop"`；`needs_human` facet；detail 含 `status=needs_human` 與 summary 原文；`evidence_refs == [log_path]`；`model_diagnostics["details.host_preflight_status"]` 等於 fixture 值（fixture 取 issue 本文字串原樣，含 `…`；不對照實機 log） |
| verify failed、details 字串、`reports == []` | 同上 | 被認定；`model_diagnostics["details"]` 原文 |
| review failed ± `authority_hashes` | `_review_terminalize_fixture` 樣板（複製進新檔） | `reason == "review-terminal-explicit-stop"`；detail 含 reason 原文；`model_diagnostics["findings[0]"]` 存在 |
| 不耗額度／不回派／不授權 | 同上 | `attempts` 不變；run 的 job 數不變；`workflow_evidence is None`；`candidate_head`／`verified_head`／`gate_result` 不變；`authority_granted is False` |
| operator resume 冪等 | 落地後再 `operator_resume=True` | 同一個 reason、job 數不變、不 raise |
| periodic tick 不覆寫 | `manager_daemon.build_periodic_tick_runner` 一個 tick（#922 periodic 樣板） | `needs_human_reason.reason` 不是 `resume-workflow-failed` |
| next_actions 有出路 | `manager.workflow_status_entry(registry, run)` | `{"abandon", "retry-card"} ⊆ next_actions` |
| sandbox 回收／drift | monkeypatch `manager._discard_reviewer_sandbox`（記錄呼叫／擲 `ValueError`） | 正常路徑以 `require_candidate_unchanged=True` 呼叫；drift → `reviewer-candidate-drift`、不 raise |
| 形狀反例 | 空 summary、多一鍵、`schema_version: True`、phase／kind 錯配、review 多出未知鍵、`reports: [42]`、report 列缺 `body`、review `findings: [42]` | predicate 回 `None`；resume 仍 `pytest.raises(ValueError)` |
| 既有路徑 | plan／build job；verify／review envelope | 新 predicate 對 plan／build 回 `None`；`_retryable_nonpassing_workflow_terminal` 對 verify／review 仍 `False`；既有 #717／#261／#880／#922／wiring 測試原斷言全綠 |

### D8 Sizing

Production 只有 `paulsha_cortex/coordinator/manager.py` 一個模組 → `domain_breadth=0`。新分支只經既有 `_manager_update_workflow_run` 對單一 run 寫一次 bounded 欄位（facets／gate_status／needs_human_reason），外加既有的 reviewer sandbox 回收；不新增 durable 欄位，也沒有跨物件 CAS 或 crash window → `state_consistency=1`。三件齊全時機械三維固定 4，總分 5，屬 Yellow。
