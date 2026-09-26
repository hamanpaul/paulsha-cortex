---
status: accepted
work_item: closure-todo-advisory
---

# Remote closure 與 Monitor 投影的 Todo checkbox 降為 advisory 設計

## Decisions

### D1 checkbox 是 builder 自述，不是 closure authority

`evaluate_remote_closure` 刪除 `if not facts.todo_complete: reasons.append("todo-incomplete")` 兩行，其餘判式逐字保留。

理由：完成度的權威在 merge 前已成立。exact candidate 必須通過 verify 卡與 ForeignReview（reviewer 以 pinned spec／design／todo 為輸入），而 CompletionRecord 以 `verification_hash`／`review_evaluation_hash`／`candidate` 綁定這些 evidence。`verify_remote_closure` 在 merge 後另外驗 `candidate == expected_head`、`target_ref_sha == default_head`，以及 `completion_records_semantically_match`。

checkbox 則兩面失真，也違反 #261 R2「自述不構成授權」：
- builder 可以不做事就翻勾（可偽造）。
- builder 做完卻沒翻勾（誤擋）。main 上 59／102 份 workstream todo 全為 `[ ]`。

`fetch_remote_closure` 不動：Todo 必須存在於 default head、payload 形狀 fail-closed，`todo_complete`／`todo_revisions` 照算，作為觀測事實。

WorkflowRun 的 terminal transition 不在本票改動範圍。daemon 路徑由 `manager.apply_workflow_action` 的 review→ship `advance` 在拿到 ship validator 的 trusted completion 後寫 `status="done"`（`manager.py:12477`、`:12538-12580`）；只有 `canonical_run.current_phase == "ship"` 時才由 `_ship_action` 自己寫（`work_actions.py:5683`）。#1086 已補上共同的 outcome-first 順序；本票兩條路徑只改 Todo closure 不再擲例外。

### D2 advisory 的可觀測面：只記 warning

`_ship_action` merged 分支在 `verify_remote_closure` 回傳後以 `getattr(closure.facts, "todo_complete", None)` 取值。值為 `False` 時：

```python
logger.warning(
    "remote closure todo advisory: unchecked items run_id=%s work_id=%s todo_paths=%s merge_commit=%s",
    ...,
)
```

用 `getattr` 的原因：既有測試的 fake orchestrator 回 `SimpleNamespace(merge_commit=...)`，沒有 `todo_complete` 屬性（`tests/test_engineering_outcome.py` 的 `Orchestrator.verify_remote_closure`、`tests/test_work_actions.py` 兩處同型），直接取屬性會打破既有斷言。

不寫進 engineering outcome：不新增 `todo_complete` 欄位，避免改 durable outcome 形狀；#1086 已讓 review→ship 在終態轉換前寫入既有 shipped outcome，但不把 Todo advisory 狀態加入 payload。

不寫進 CompletionRecord：`completion.validate_completion_record` 是 strict schema，加欄位要 bump schema，超出範圍。cached `done` 重播路徑不加 warning，避免每次 refresh 重複記同一件事。

### D3 builder 勾選目標由 planning authority 導出

#310 的卡片契約已要求 builder 勾 tasks／todo（`manager._checkbox_insensitive_equal` docstring），但無 OpenSpec 的 run 在 prompt 裡只看到 `<change>` 佔位路徑，外加「never modify pinned input files such as the plan document」，builder 因此不碰 workstream todo。

修法：在 `tasks_path` 計算處加一個分支。

```python
if isinstance(contract.get("openspec_ref"), str):
    tasks_path = <現行 openspec 路徑>              # 逐字不變
else:
    todo_refs = sorted({
        item.ref for item in getattr(run, "planning_authority", ())
        if item.kind == "plan" and Path(item.ref).name in _CHECKBOX_VOLATILE_PLAN_BASENAMES
    })
    tasks_path = " and ".join(todo_refs) if todo_refs else <現行 <change> 佔位字串>
```

句子其餘文字不變。有 OpenSpec 的 run、無 planning authority 的舊 run、fake `SimpleNamespace` run 的 prompt 都逐字不變。

builder 翻勾不會觸發 authority restart：`claim.semantic_source_revision` 對 `todo` 只取 `identity:{ref}`，Todo 內容變動不改 authority digest；reviewer 的 frozen authority 驗證由 #310 的 `_authority_map_with_checkbox_tolerance` 容忍 checkbox-only 差異。

### D4 `retire-delivered` 的宣告用本地 durable 事實

`_phase_recovery_actions` 的原則是「只宣告會被受理的動作、拿不準就不宣告、不打網路」（#546／#382）。`retire-delivered` 的受理條件是 run `ongoing`、沒有 active job、有 `pr_refs` 且全部 terminal、沒有其他 ongoing run。

「PR terminal」用 journal 事實代替 GitHub 查詢：journal `ship.phase` 只在 authenticated `fetch_merge_status.merged` 且 `pr_head` 等於授權 HEAD 之後才寫成 `"merged"`（`work_actions.py:5814-5823`、`:5897-5947`、`:6279-6297`，以及 maintainer 路徑 `:1492-1500`）。merge 不可逆，所以 PR 必為 terminal；`_retire_delivered_pr_terminal_status` 的 live 查詢必過。

實作形狀：新增私有 helper `_retire_delivered_exposable(run, workflow_registry, *, state_path) -> bool`。
- 以 `_load_runs(state_path)` 讀取，整段包在 `try/except (OSError, ValueError, TypeError, KeyError)` 內，任何例外都回 False。
- 依序檢查 R5 (i)–(v)。
- `_phase_recovery_actions` 在 `review-attest` 判斷之後、`return` 之前呼叫它。

只有 `_claim_action` 的 resume 回應會傳 `state_path`，既有兩個位置參數呼叫點行為不變。

### D5 Monitor strict closure 對齊 coordinator remote closure

只改 R1 不夠。run 收斂為 `done` 後，`project_work_items` 不再把它算成 active workflow。`reduce_lifecycle`（`monitor/lifecycle.py:82-93`）接著只剩 `strict_closure` 一條路到 `done`，否則落到 `active_todo` → `todo`。而 `_parse_closure_evidence` 有兩處讓 #810 的兩種 work item 永遠過不了 `strict_closure`：
- `todo_tasks_complete` 要求 workstream Todo `complete is True`（`work_api.py:1100-1110`）：#802 型（有 OpenSpec）卡在這裡。
- `remote_active_openspec_absent`／`remote_archive_present` 以 `bool(openspec_refs) and ...` 計算（`work_api.py:1093-1099`），無 OpenSpec 恆為 `False`：#819 型卡在這裡。`docs/unified-work-lifecycle.md:12` 早已宣稱無 mapped OpenSpec 時不要求 archive，程式沒有跟上。

所以只修 R1 的話，#810 的 work item 會從 `on-going` 變成 `todo`（連 `next_actions: ["start"]` 都會回來），比現況更誤導。

修法（只動 `_parse_closure_evidence` 的兩段計算）：

```python
if observations.get("remote_openspec_observed") is True:
    combined[wid]["remote_active_openspec_absent"] = (not openspec_refs) or all(ref not in active for ref in openspec_refs)
    combined[wid]["remote_archive_present"] = (not openspec_refs) or all(ref in archived for ref in openspec_refs)
...
combined[wid]["todo_tasks_complete"] = (
    bool(todo_evidence)
    and all(todo["complete"] is True for todo in openspec_todos)
    and openspec_tasks_complete
)
```

有 OpenSpec ref 時兩個 OpenSpec 欄位的判式等價於現行；archived OpenSpec tasks 的全勾要求保留（#808 的 archive gate 保證它成立）；workstream Todo 只要求存在，對應 coordinator「Todo 必須存在於 default head」。`ClosureEvidence`、`reduce_lifecycle`、providers 都不動，`test_partial_closure_never_projects_done` 在 reducer 層的語意不變。

不會誤判 `done`：closure row 只在已有 validated CompletionRecord（或 provider 明示的 closure row）時才建立（`work_api.py:1005`），且 `completion_record_valid`、`issues_all_closed`（至少一個 confirmed issue）、`pr_merged_with_merge_commit`（至少一個 confirmed PR）的判式不變。無 OpenSpec 的放寬只在 `remote_openspec_observed is True` 時生效，provider degraded 時維持 `False`。

與 #895 的分界：#895 處理「沒有 CompletionRecord 的已交付 work item」與 superpowers source 恆為 `active`，其非目標明列 #810。D5 只改有 CompletionRecord 時的 closure 計算，兩者不重疊。

### D6 否決的替代方案

- **merge 前 checkbox blocking gate（pr-preflight）**：會把 merge 後的死結換成 merge 前的 needs_human。只為了翻勾就得跑一次完整 `retry-build`（build＋verify＋review）；已建好、未翻勾的在飛候選會全部停住；而判準仍是自述。
- **Manager 在 local-closeout 自動翻勾**：無 OpenSpec 的 run 目前沒有 local-closeout commit（`work_bridge.py:1920`）。新增 commit 會產生新 candidate，要比照 `_commit_archive_and_require_reverification` 做 harvest、registry reset 與 reverification，屬 cross-object durable（`state_consistency=2`），超出 Yellow。列為後續，可與 #808 合併處理。
- **Monitor 改成看 WorkflowRun `done` 直接投影 `done`**：會繞過 remote truth（merge ancestry、issue closure、OpenSpec archive），違反 `README.md:386` 以遠端 default branch 事實判 `done` 的契約，也和 #895 的設計空間重疊。
- **engineering outcome 加 `verification.todo_complete`**：會改 durable 形狀；Todo checkbox advisory 仍只記 warning，不加入 payload。

### D7 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| closure 放行未勾 Todo | `RemoteClosureFacts` 其餘成立、`todo_complete=False` | `allowed`、`reasons == ()` |
| 其餘 gate 未被放寬 | 逐一把 ancestry／merge commit／issue／openspec／completion 設為失敗 | blocked，reason 不含 `todo-incomplete` |
| orchestrator 真實路徑 | `test_delivery_orchestrator.py` 樣板，fake `fetch_remote_closure` 回 `todo_complete=False` | CompletionRecord 寫出並回讀、`result.facts.todo_complete is False` |
| ship 端到端 | `test_engineering_outcome.py` ship e2e 樣板，fake closure facts 帶 `todo_complete=False` | `action == "done"`、run `done`、warning、outcome `verification` 不含 `todo_complete` |
| 舊 fake 相容 | facts 無 `todo_complete` 屬性 | 不擲、無 warning |
| prompt 目標 | `SimpleNamespace` run 帶 `PlanningArtifactAuthority(kind="plan", ref=".../todo.md")`、無 openspec | 含 `<ref> checkboxes`、不含 `<change>`；有 openspec 時由既有測試保證不變 |
| retire 宣告 | 真 `JobRegistry`＋journal fixture | merged／done＋PR 一致 → 含；其餘七種情形 → 不含、不擲 |
| resume 回應 | `_claim_action` resume，run 已 merge 且 needs_human | `next_actions` 既有值在前、`retire-delivered` 在後 |
| Monitor 有 OpenSpec＋Todo 未勾 | `_run_closure_projection` 樣板，workstream Todo `complete: False` | `done` |
| Monitor 無 OpenSpec＋Todo 未勾 | 同上，override 只連 issue／PR，remote_todos 只有 workstream Todo | `done` |
| Monitor 未被放寬過頭 | 無 Todo evidence／observed 缺席／issue open／PR 非 merge commit／archived tasks 未勾／無 completion | 皆非 `done` |

### D8 Sizing

4 個 production 模組（`github_delivery.py`、`work_actions.py`、`manager.py`、`monitor/work_api.py`）→ `domain_breadth=2`。

`state_consistency=0`：
- D1 與 D5 是純判式。
- D2 只寫 log，不寫任何 durable 物件。
- D3 只改 prompt 文字。
- D4 只讀 delivery journal 與 registry，不寫、不新增持久狀態；讀取失敗一律不宣告。

三件齊全時機械三維固定 4，總分 6／Yellow。
