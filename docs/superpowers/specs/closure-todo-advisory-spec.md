---
status: accepted
work_item: closure-todo-advisory
---

# Remote closure 與 Monitor 投影的 Todo checkbox 降為 advisory、builder 勾選目標與已 merge run 的 retire-delivered 出口規格

## Requirements

對應 [#810](https://github.com/hamanpaul/paulsha-cortex/issues/810)：remote closure 在 merge 之後要求 default branch 上的 mapped workstream Todo 全部 `[x]`，但交付管線沒有任何一環會勾它（builder prompt 指向 `<change>` 佔位路徑、ship 的 local-closeout 只處理 OpenSpec），PR 已 merge、issue 已關的 run 因此永遠停在 `remote closure blocked: todo-incomplete`，而 `next_actions` 只給必定被拒的 `abandon`。Monitor 的 strict closure 也要求同一份 Todo 全勾，且對無 OpenSpec 的 work item 恆判 OpenSpec 未 archive，所以即使 run 收斂，work item 仍投影不到 `done`。完成度的權威改由 merge 前 exact candidate 的 verify／ForeignReview evidence 承擔（已由 CompletionRecord 綁定），merge 後的 workstream Todo checkbox 狀態只作 advisory 觀測。

1. **R1 remote closure 不以 Todo checkbox 狀態阻擋**：`github_delivery.evaluate_remote_closure` 不再因 `facts.todo_complete is False` 產生 `todo-incomplete`。其餘 reason（`remote-default-unverified`、`merge-ancestry-unverified`、`merged-pr-head-unverified`、`merge-commit-required`、`issue-not-closed`、`active-openspec-present`、`openspec-archive-missing`、`completion-record-invalid`）的判式、產生順序與 `_unique_reasons` 去重完全不變。`RemoteClosureFacts` 欄位不增不減（`todo_complete`、`todo_revisions` 保留）。`GitHubDeliveryClient.fetch_remote_closure` 不改：`todo_paths` 為空仍 `ValueError`、Todo 路徑不安全仍 `ValueError`；default head 上 Todo payload 非 file／非 base64／sha 非 40-hex／內容非 UTF-8 仍 `RuntimeError`（Todo 必須存在於 default head 的 fail-closed 保留）；`todo_complete` 仍依現行 regex 計算，作為觀測事實回傳。
2. **R2 已 merge run 自行收斂為 done**：journal `ship.phase == "merged"` 且其餘 remote facts 成立（雙親 merge commit 為 default head 祖先、PR head 等於授權 HEAD、mapped issues 全 closed、有 mapped OpenSpec 時 archive 成立且 active change 消失、CompletionRecord 語意一致）時，即使 default branch 上 mapped Todo 仍有 `- [ ]`，`ShipOrchestrator.verify_remote_closure` 必須寫出並回讀 CompletionRecord 成功，`work_actions._ship_action` 的 merged 分支把 journal ship 推到 `phase: "done"` 並回傳 `action == "done"`，不再擲 `remote closure blocked: todo-incomplete`。WorkflowRun 的 terminal transition 沿用現行兩條路徑、本票不改：
   - daemon 路徑：`manager.apply_workflow_action` 的 review→ship `advance` 在 run 仍為 `current_phase == "review"` 時呼叫 ship validator；remote closure 成功後先 durable 寫入並讀回唯一 shipped outcome，再回傳 trusted completion，由 manager 寫 `current_phase="ship"`、`status="done"`、`merge_revision`、`completion_record_path`／`completion_record_hash`。
   - `canonical_run.current_phase == "ship"` 時（operator 直接 `cortex work ship`）：由 `_ship_action` 先 emit `shipped` outcome，再寫同一組欄位。

   cached `done` 重播路徑同樣不因 Todo 未勾失敗。
3. **R3 advisory 可觀測、不影響 gate**：`_ship_action` merged 分支取得 `closure` 後，以 `getattr(closure.facts, "todo_complete", None)` 取值。值為 `False` 時 `logger.warning` 一行，含 `run_id`、`work_id`、`todo_paths`、`merge_commit`。值為 `True` 或非 bool（舊 fake、缺屬性）時不記 warning，也不擲例外。cached `done` 重播分支不加 warning。此值不進 CompletionRecord、`delivery_binding`、`merge_authorization`、engineering outcome（`emit_outcome` 的 `verification` 維持只有 `todo_paths`），也不改 `_ship_action` 的回傳 dict。
4. **R4 builder 勾選目標指向真實 Todo**：`manager._workflow_job_prompt` 的 commit-required 卡（`effective_commit_policy == "required"`）：`contract` 帶 `openspec_ref` 時勾選句逐字不變。沒有 `openspec_ref` 時，若 `getattr(run, "planning_authority", ())` 內有 `kind == "plan"` 且 `Path(ref).name ∈ _CHECKBOX_VOLATILE_PLAN_BASENAMES` 的 ref，`tasks_path` 改為這些 ref（去重、排序、以 `" and "` 連接），句型「Before the final commit, update {tasks_path} checkboxes for work completed by this card, and never modify pinned input files such as the plan document.」其餘文字不變。兩者皆無時維持現行 `<change>` 佔位路徑（既有 `test_commit_required_build_card_without_openspec_ref_uses_generic_tasks_path` 保持綠）。非 commit-required 卡（worktree-isolation、verify／review 的 reviewer 卡、planner 卡）prompt 逐字不變。
5. **R5 已 merge run 的 needs_human 出口宣告 `retire-delivered`**：`work_actions._phase_recovery_actions` 新增 keyword-only 參數 `state_path: Path | None = None`。`state_path` 有提供且下列條件全部成立時，在回傳尾端補 `"retire-delivered"`（既有 `regenerate-gates`／`retry-card`／`review-attest` 在前、不重複）：(i) `run.status == "ongoing"` 且 facets 含 `needs_human`；(ii) registry 內沒有該 run 的 active job（`ACTIVE_JOB_STATUSES`）；(iii) 同 repo／work_id 沒有其他 `status == "ongoing"` 的 WorkflowRun；(iv) `run.pr_refs` 恰一筆，且等於 `f"{run.repo}#{pr_number}"`，其中 `pr_number` 取自 delivery journal `runs[run.run_id]["delivery_binding"]["pr_number"]`；(v) 同一 row 的 `ship.phase ∈ {"merged", "done"}`。journal 不存在、讀取或解析失敗、形狀不符時一律不宣告，也不擲例外。`_claim_action` 的 resume 回應組裝（`for item in _phase_recovery_actions(...)` 處）改傳 `state_path=state_path`；refreeze-base 的呼叫點不改。`_retire_delivered_action` 與 registry `_manager_validate_workflow_retire_delivered` 的受理條件不改。
6. **R6 Monitor strict closure 與 coordinator remote closure 對齊**：`monitor/work_api._parse_closure_evidence` 只改兩處計算，`ClosureEvidence` 欄位、`monitor/lifecycle.reduce_lifecycle` 的規則順序、providers 的 `remote_todos`／`remote_openspec` 產出都不改：
   - (a) `todo_tasks_complete`：workstream Todo（`remote_todos` 中 `work_id == group.work_id` 的列）只要求存在，不再要求 `complete is True`。OpenSpec archived tasks（`openspec_ref ∈ openspec_refs` 的列）仍要求全部 `complete is True`，`openspec_tasks_complete` 的覆蓋判定不變。也就是 `bool(todo_evidence) and all(todo["complete"] is True for todo in openspec_todos) and openspec_tasks_complete`。
   - (b) `remote_openspec_observed is True` 且 group 沒有任何 confirmed OpenSpec ref 時，`remote_active_openspec_absent` 與 `remote_archive_present` 為 `True`，對應 coordinator 的 `openspec_required=False`；有 OpenSpec ref 時判式逐字不變；`remote_openspec_observed` 不為 `True` 時維持不寫入（預設 `False`）。
   - 其餘三個 closure 欄位（`pr_merged_with_merge_commit`、`issues_all_closed`、`completion_record_valid`）的判式，以及 closure row 只在 `combined` 已有或有 validated completion 時才建立的條件（`work_api.py:1005`），逐字不變。結果：run 收斂為 `done` 且 CompletionRecord 有效後，以 workstream Todo 為來源的 work item（有或無 OpenSpec）即使 Todo 未勾，也投影為 `done`；沒有 CompletionRecord、issue 未關、PR 未以 merge commit 合入時仍不投影 `done`。
7. **R7 測試**：新增 `tests/test_closure_todo_advisory.py` RED→GREEN：
   - (a) `evaluate_remote_closure` 在其餘 facts 成立、`todo_complete=False` 時 `allowed` 且 `reasons == ()`；其餘任一 fact 失敗仍 blocked，且 reasons 不含 `todo-incomplete`。
   - (b) `ShipOrchestrator.verify_remote_closure` 在 fake `fetch_remote_closure` 回 `todo_complete=False` 時成功寫出並回讀 CompletionRecord。此案可放在 `tests/test_delivery_orchestrator.py`，沿用該檔 `_authority`／`_foreign_review` helper 與 `test_remote_closure_reads_and_validates_completion_record` 樣板。
   - (c) `execute_work_action` ship 端到端，沿 `tests/test_engineering_outcome.py::test_ship_action_emits_shipped_outcome_before_terminal_transition` 樣板：fake closure 的 `facts.todo_complete=False` → `action == "done"`、run `status == "done"`、caplog 有含 run_id 的 warning、outcome `verification == {"todo_paths": [...]}`（不含 `todo_complete`）。facts 無該屬性時同樣 done、無 warning、不擲例外。
   - (d) prompt：no-openspec run 帶 `kind="plan"`、basename `todo.md` 的 `planning_authority` ref → commit-required 卡的 prompt 含 `<該 ref> checkboxes`，且不含 `<change>`；只有 spec／design，或只有 basename 非 `todo.md`／`tasks.md` 的 plan ref → 維持佔位；worktree-isolation 卡與 reviewer 卡不含勾選句。
   - (e) `_phase_recovery_actions`：journal `ship.phase="merged"` 且 binding PR 與 `run.pr_refs` 一致 → 含 `retire-delivered`。下列情況都不含且不擲：`phase="review-requested"`、PR 不一致、`pr_refs` 為空、有 active job、另有 ongoing run、journal 損毀、未給 `state_path`。
   - (f) `resume` 回應對已 merge 且 needs_human 的 run，`next_actions` 既有值在前、`retire-delivered` 在後。
   - (g) Monitor 投影，沿 `tests/test_monitor_work_review_regressions.py` 的 `_run_closure_projection`／`test_production_wiring_passes_workflow_links_and_strict_closure` 樣板（真 `WorkModelRefresher`＋static providers＋validated completion）：
     - (g1) 有 OpenSpec、archived tasks `complete: True`、workstream Todo `complete: False` → `done`。
     - (g2) 無 OpenSpec、只有 issue／PR／workstream Todo `complete: False` → `done`。
     - (g3) 下列都不投影 `done`：無 OpenSpec 且沒有任何 Todo evidence；無 OpenSpec 但 `remote_openspec_observed` 缺席；issue 未關；PR 未以 merge commit 合入；有 OpenSpec 但 archived tasks `complete: False`；沒有 validated completion。

   既有測試的唯一允許改動：`tests/test_github_delivery.py::test_remote_closure_is_strict_conjunction` 的 blocked 迴圈移除 `"todo_complete"`，改為斷言 `todo_complete=False` 仍 allowed。其餘既有斷言全數保留，包括 `tests/test_github_delivery_client.py` 的 `facts.todo_complete` 觀測斷言、`tests/test_delivery_orchestrator.py`、`tests/test_work_actions.py`、`tests/test_engineering_outcome.py`、`tests/test_coordinator_manager.py`（`WorkflowJobPromptTests`）、`tests/test_reviewer_card_retry_569.py`、`tests/test_midchain_builder_retry_545.py`、`tests/test_ship_lane_no_openspec_911.py`、`tests/test_copilot_review_adopt_existing.py`、`tests/test_monitor_work_lifecycle.py`（含 `test_partial_closure_never_projects_done`）、`tests/test_monitor_work_review_regressions.py`、`tests/test_monitor_work_providers.py`、`tests/test_monitor_git_native_reads_506.py`。

## Boundary

Production 只改四個模組：
- `paulsha_cortex/coordinator/github_delivery.py`：`evaluate_remote_closure`。
- `paulsha_cortex/coordinator/work_actions.py`：`_ship_action` merged 分支的 advisory warning、`_phase_recovery_actions`、`_claim_action` 呼叫點。
- `paulsha_cortex/coordinator/manager.py`：`_workflow_job_prompt` 的 `tasks_path`。
- `paulsha_cortex/monitor/work_api.py`：`_parse_closure_evidence` 的 `todo_tasks_complete` 與無 OpenSpec 時的兩個 OpenSpec 欄位。

`delivery.py` 不改（`verify_remote_closure` 流程與兩次 gate 評估不動）；`fetch_remote_closure`、CompletionRecord schema（`completion.py`）、`engineering_outcome.py`、`delivery_binding`／`merge_authorization` 形狀、`monitor/lifecycle.py`、`monitor/providers.py` 都不改。

以下不在本票範圍：
- 不做 Manager 在 local-closeout 自動翻勾 Todo：需要新 commit，進而觸發 reverification、registry reset、spool harvest，屬 cross-object durable 狀態；可與 #808 的「ship 自勾」合併另票。
- 不新增 merge 前的 checkbox blocking gate。
- 不改 archive gate 對 OpenSpec tasks 全勾的要求（#808）；Monitor 對 archived OpenSpec tasks 的全勾要求同樣保留。
- 不在 engineering outcome 加 `todo_complete`：Todo advisory 只透過 warning 與 default branch 內容觀測，不改 outcome durable 形狀；#1086 只補 review→ship 的 outcome 寫入順序與重入，不擴充 schema。
- 不改 Monitor 的 superpowers source 恆為 `active`、無 CompletionRecord 的已交付 work item 投影（#895，該票非目標明列 #810）。本票的 R6 只處理「有 validated CompletionRecord」的 closure 計算。
- 不改 `claim._resume_decision` 宣告 `abandon` 的行為。
- 不改 `abandon`／`retire-delivered` 的受理條件。
- 不處理 #810 原文第 4 點（issue 關閉後 `retry-build` 的 authority row-malformed）；2026-09-21 實例已確認 `retire-delivered` 在 issue 關閉後可用。
- 不新增 CLI。

## Evidence

- #810 原文（cortex 0.1.8、main `0f57165c`）：run `workflow-44a15826ed852d8f3be5` 的 PR #809 已 merge、issue #802 已關，仍停在 `RuntimeError: remote closure blocked: todo-incomplete`，`merge_revision` 為 `None`，work item 停在 `on-going`、永遠不投影 `done`。該 work item 有 OpenSpec（`openspec/changes/archive/2026-08-27-planning-artifact-manifest-binding/`）。
- 2026-09-21 留言（pin `442fe23f`）：#819 run `workflow-e75b36c500fb15fde025` 由 Manager 自行 merge PR #947（`5450c053`），下一個 tick 在 `delivery.verify_remote_closure` 擲同一錯誤，run 落入 `resume-workflow-failed` needs_human，`next_actions` 只給 `abandon`。候選 `aa50f258` 的 `docs/superpowers/workstreams/daemon-tick-clock-not-idle/todo.md` 14 項全 `[ ]`，該 work item 無 OpenSpec；#849（`e839aef1`，5 項）與 #862（`4e043feb`，6 項）同型。operator 以 `retire-delivered` 收尾，另見 `docs/handoffs/2026-09-23-refine-b2-handoff.md:19,99`。
- main `7fa4716b` 現行程式：
  - `github_delivery.py:254-282`：`evaluate_remote_closure` 在 :276-277 對 `not facts.todo_complete` 加 `todo-incomplete`。
  - `github_delivery.py:817-855`：`fetch_remote_closure` 對任何 `[ ]` 或無 task 設 `todo_complete = False`（:853-854）。
  - `delivery.py:623-629`、`:680-686`：`verify_remote_closure` 兩次評估，失敗即 `RuntimeError("remote closure blocked: ...")`。
  - `work_actions.py:5637-5730`：merged 分支，closure 在 :5659；`shipped` outcome（`verification={"todo_paths": ...}` 在 :5706）與 WorkflowRun `status="done"` 只在 :5683 `canonical_run.current_phase == "ship"` 時執行。
  - `manager.py:12453-12581`：daemon 的 review→ship `advance` 在 :12477 呼叫 ship validator（run 仍為 review），:12538-12580 才寫 `current_phase="ship"`、`status="done"`、completion 欄位。
  - `work_actions.py:2522-2599`：`_phase_recovery_actions` 只可能宣告 `regenerate-gates`／`retry-card`／`review-attest`，由 :2172 的 resume 回應消費。
  - `work_actions.py:3827-3952`：`_retire_delivered_action` 要求 `pr_refs` 全部 terminal。
  - registry `_manager_validate_workflow_abandon`（`registry.py:2847`）只收 pre-delivery run，已 merge 的 run 宣告 `abandon` 必被拒。
  - `manager.py:10346-10370`：無 `openspec_ref` 時 `tasks_path` 為 `<change>` 佔位字串，並附「never modify pinned input files such as the plan document」。
  - `manager.py:6050-6064`：#310 已容忍 `tasks.md`／`todo.md` 的 checkbox-only 差異；`claim.semantic_source_revision` 對 `todo` 只取 `identity:{ref}`，Todo 內容變動不改 authority digest。
  - `work_bridge.py:1920`：local-closeout 只在 active OpenSpec change 目錄存在時執行。
  - `monitor/work_api.py:1093-1099`：`remote_active_openspec_absent`／`remote_archive_present` 以 `bool(openspec_refs) and ...` 計算，無 OpenSpec 的 work item 恆為 `False`；`:1100-1110`：`todo_tasks_complete` 要求 workstream Todo 與 archived tasks 全部 `complete is True`。
  - `monitor/lifecycle.py:82-93`：run 非 active 後，`done` 只能經 `strict_closure`（`ClosureEvidence.complete` 六欄全真），否則落到 `active_todo` → `todo`。因此 R1 單獨修好後，#810 的 work item 會從 `on-going` 變成 `todo`，不會變成 `done`。
  - `docs/unified-work-lifecycle.md:12` 已宣稱「沒有 mapped OpenSpec 時則不要求 archive」，與 `work_api.py:1093-1099` 不一致。
  - #895 的非目標明列「#810 的 todo-incomplete（已有 run 的 remote closure）——同型但另票」，其驗收只涵蓋無 CompletionRecord 的 work item。
  - main 上 102 份 workstream `todo.md` 中，75 份仍有未勾項、59 份全為 `[ ]`。
