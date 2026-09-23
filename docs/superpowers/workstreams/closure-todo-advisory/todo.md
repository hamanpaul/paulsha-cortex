---
status: accepted
work_item: closure-todo-advisory
domain_breadth: 2
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Remote closure 與 Monitor 投影的 Todo checkbox 降為 advisory、builder 勾選目標與 retire-delivered 出口（#810）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#810`；[spec](../../specs/closure-todo-advisory-spec.md)、[design](../../specs/closure-todo-advisory-design.md)。
- 觸及模組（4 個 production 模組 → `domain_breadth: 2`）：
  - `paulsha_cortex/coordinator/github_delivery.py`：`evaluate_remote_closure`。
  - `paulsha_cortex/coordinator/work_actions.py`：`_ship_action` merged 分支的 advisory warning、`_phase_recovery_actions`、`_claim_action` resume 回應呼叫點。
  - `paulsha_cortex/coordinator/manager.py`：`_workflow_job_prompt` 的 `tasks_path`。
  - `paulsha_cortex/monitor/work_api.py`：`_parse_closure_evidence` 的 `todo_tasks_complete` 與無 OpenSpec 時的兩個 OpenSpec 欄位。
- `state_consistency: 0`：closure 與 Monitor closure 判式是純邏輯；advisory 只寫 log；prompt 只改文字；delivery journal 與 registry 只讀，不寫、不新增持久狀態。
- 不改的模組與形狀：`delivery.py`、`fetch_remote_closure`、CompletionRecord schema、`engineering_outcome.py`（outcome `verification` 維持只有 `todo_paths`）、`delivery_binding`／`merge_authorization` 形狀、`monitor/lifecycle.py`（`ClosureEvidence`、`reduce_lifecycle`）、`monitor/providers.py`、`_retire_delivered_action`／`abandon` 的受理條件、`claim._resume_decision`。
- 本票不做：
  - Manager 在 local-closeout 自動翻勾（新 commit → reverification，另票，可併 #808）。
  - merge 前的 checkbox blocking gate。
  - archive gate 的 OpenSpec tasks 全勾（#808）；Monitor 對 archived OpenSpec tasks 的全勾要求同樣保留。
  - engineering outcome 加 `todo_complete`（daemon 的 review→ship 路徑不 emit `shipped` outcome，且會改 durable 形狀）。
  - Monitor 的 superpowers source 恆 `active`，以及無 CompletionRecord 的已交付 work item 投影（#895）。
  - #810 原文第 4 點（issue 關閉後 `retry-build` 的 row-malformed）。
  - 新增 CLI。
- 既有測試只允許一處斷言改動：`tests/test_github_delivery.py::test_remote_closure_is_strict_conjunction` 的 blocked 迴圈移除 `"todo_complete"`，改為斷言 `todo_complete=False` 仍 allowed。其餘既有斷言不得修改。
- spec／design／本 todo 文字是 pinned authority，只准把 `[ ]` 翻成 `[x]`；澄清寫進 terminal reason。
- 留在 Manager checkout 的分支上工作，不得另建 `wt/...` 分支。
- 不得 commit 或刪除 `docs/superpowers/plans/closure-todo-advisory.md`。
- 測試與文件不得硬編 `openspec/changes/<change>/` 路徑；prompt 測試以「不含 `<change>`」斷言佔位已被取代。

## 現場證據

- #810 原文：run `workflow-44a15826ed852d8f3be5` 的 PR #809 已 merge（`0f57165c`）、issue #802 已關，仍卡在 `remote closure blocked: todo-incomplete`，`merge_revision` 為 `None`，work item 停在 `on-going`。該 work item 有 OpenSpec。
- 2026-09-21 留言：#819 run `workflow-e75b36c500fb15fde025` 由 Manager 自行 merge PR #947（`5450c053`），下一 tick 同錯，落入 `resume-workflow-failed`，`next_actions` 只給 `abandon`。候選 `aa50f258` 的 todo 14 項全 `[ ]`，該 work item 無 OpenSpec；#849（5 項）、#862（6 項）同型。operator 以 `retire-delivered` 收尾。
- main `7fa4716b`：
  - `github_delivery.py:276-277` 對未勾 Todo 加 `todo-incomplete`。
  - `delivery.py:629`、`:686` 擲 `remote closure blocked`。
  - `work_actions.py:5683`：`shipped` outcome 與 WorkflowRun `done` 只在 `current_phase == "ship"` 時由 `_ship_action` 寫；daemon 路徑由 `manager.py:12477`、`:12538-12580` 的 review→ship `advance` 寫。
  - `_phase_recovery_actions`（`:2522-2599`）不會宣告 `retire-delivered`。
  - `manager.py:10346-10370` 在無 OpenSpec 時給 builder 的是 `<change>` 佔位路徑，外加「never modify pinned input files such as the plan document」。
  - `monitor/work_api.py:1093-1099` 對無 OpenSpec 的 work item 恆判 OpenSpec 未 archive；`:1100-1110` 要求 workstream Todo 全勾；`monitor/lifecycle.py:82-93` 讓 run 收斂後只剩 strict closure 能到 `done`，否則投影 `todo`。
  - main 上 102 份 workstream todo 中，59 份全為 `[ ]`。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_closure_todo_advisory.py`，逐條對應 spec R7 (a)、(c)、(d)、(e)、(f)、(g)。(b) 加在 `tests/test_delivery_orchestrator.py`，沿用該檔 `_authority`／`_foreign_review` 與 `test_remote_closure_reads_and_validates_completion_record` 樣板。(c) 沿 `tests/test_engineering_outcome.py::test_ship_action_emits_shipped_outcome_before_terminal_transition` 樣板。(e) 用真 `JobRegistry` 加 journal fixture。(g) 沿 `tests/test_monitor_work_review_regressions.py` 的 `_run_closure_projection` 樣板。現行 main 必須 RED：(a) reasons 含 `todo-incomplete`；(b) 擲 `remote closure blocked: todo-incomplete`；(c) caplog 無 advisory warning（fake orchestrator 不經 `evaluate_remote_closure`，現行會 done 但不記 warning）；(d) prompt 含 `<change>`；(e)、(f) 無 `retire-delivered`；(g1)、(g2) 不是 `done`。
- [ ] **T2 source／closure 判式（R1、R2、D1）**：`github_delivery.evaluate_remote_closure` 刪除 `todo-incomplete` 的兩行，其餘 reason 的判式、順序與去重逐字保留。`RemoteClosureFacts` 與 `fetch_remote_closure` 不改。同步把 `tests/test_github_delivery.py::test_remote_closure_is_strict_conjunction` 迴圈中的 `"todo_complete"` 移出，改為斷言 `replace(facts, todo_complete=False)` 仍 allowed（本票唯一允許的既有斷言改動）。
- [ ] **T3 source／advisory warning（R3、D2）**：`work_actions._ship_action` merged 分支在 `verify_remote_closure` 之後以 `getattr(closure.facts, "todo_complete", None)` 取值。值為 `False` 時 `logger.warning` 一行，含 run_id／work_id／todo_paths／merge_commit；其他值不記、不擲。cached `done` 分支不加。不寫進 CompletionRecord、journal binding、authorization、engineering outcome，不改回傳 dict。
- [ ] **T4 source／builder 勾選目標（R4、D3）**：`manager._workflow_job_prompt` 的 `tasks_path` 在沒有 `openspec_ref` 時，改取 `getattr(run, "planning_authority", ())` 中 `kind == "plan"` 且 basename ∈ `_CHECKBOX_VOLATILE_PLAN_BASENAMES` 的 ref（去重、排序、`" and "` 連接）；取不到時維持現行佔位字串。有 `openspec_ref` 時逐字不變。勾選句其餘文字不變；非 commit-required 卡的 prompt 不變。
- [ ] **T5 source／retire-delivered 出口（R5、D4）**：新增 `_retire_delivered_exposable(run, workflow_registry, *, state_path) -> bool`，依 spec R5 (i)–(v) 判定，所有讀取或形狀錯誤一律回 False。`_phase_recovery_actions` 加 keyword-only `state_path: Path | None = None`，條件成立時在尾端補 `"retire-delivered"`（不重複）。`_claim_action` resume 回應的呼叫點傳 `state_path=state_path`；refreeze-base 呼叫點與既有位置參數呼叫行為不變。
- [ ] **T6 source／Monitor closure 對齊（R6、D5）**：`monitor/work_api._parse_closure_evidence` 只改兩處。(a) `todo_tasks_complete = bool(todo_evidence) and all(todo["complete"] is True for todo in openspec_todos) and openspec_tasks_complete`，workstream Todo 只要求存在。(b) `remote_openspec_observed is True` 時，`remote_active_openspec_absent`／`remote_archive_present` 在 group 無 confirmed OpenSpec ref 時為 `True`，有 ref 時判式等價於現行。`ClosureEvidence`、`reduce_lifecycle`、providers、其餘三個 closure 欄位與 closure row 建立條件都不改。
- [ ] **T7 tests／回歸**：下列檔案全綠，除 T2 那一處外不改斷言：`tests/test_github_delivery.py`、`tests/test_github_delivery_client.py`、`tests/test_delivery_orchestrator.py`、`tests/test_work_actions.py`、`tests/test_engineering_outcome.py`、`tests/test_coordinator_manager.py`、`tests/test_reviewer_card_retry_569.py`、`tests/test_midchain_builder_retry_545.py`、`tests/test_ship_lane_no_openspec_911.py`、`tests/test_copilot_review_adopt_existing.py`、`tests/test_work_bridge.py`、`tests/test_monitor_work_lifecycle.py`、`tests/test_monitor_work_review_regressions.py`、`tests/test_monitor_work_providers.py`、`tests/test_monitor_git_native_reads_506.py`。另補兩條斷言：「facts 無 `todo_complete` 屬性時 ship 端到端仍 done、無 warning」；「未給 `state_path` 時 `_phase_recovery_actions` 輸出與現行相同」。
- [ ] **T8 documentation／changelog／CLI help**：新增 `changelog.d/closure-todo-advisory.md` 並同步 `CHANGELOG.md [Unreleased]`。`README.md:528` 的 remote closure 段（「`mapped_openspec == ()` 時 remote closure 以 … Todo 全勾 …」）改為「Todo 必須存在於 default branch；checkbox 狀態是 advisory，未勾時只記 warning，不阻擋 done」。`docs/unified-work-lifecycle.md` ship 段同一句同步修改，並補兩句：「無 OpenSpec 的 run，commit-required builder 卡的勾選目標是 run 的 workstream todo」、「已 merge 但 closure 失敗的 needs_human run，`next_actions` 會宣告 `retire-delivered`」。`README.md:386` 的 Monitor `done` 段補一句：「workstream Todo 只需存在，checkbox 不影響 `done`；archived OpenSpec task checklist 仍須全勾；無 mapped OpenSpec 的 work item 不要求 archive」。本票不新增 CLI，`cortex work --help` 輸出不變，以 help smoke 驗證。
