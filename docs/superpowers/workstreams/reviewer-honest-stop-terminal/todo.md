---
status: accepted
work_item: reviewer-honest-stop-terminal
domain_breadth: 0
state_consistency: 1
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# verify／review 卡誠實非通過 terminal 走明示停止落地（#874）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#874`；[spec](../../specs/reviewer-honest-stop-terminal-spec.md)、[design](../../specs/reviewer-honest-stop-terminal-design.md)。
- 觸及模組：只有 1 個 production 模組，所以 `domain_breadth: 0`。
  - `paulsha_cortex/coordinator/manager.py`：新增 `_explicit_stop_gate_terminal`、病因投影 helper，以及 `resume_workflow_run` 內緊接 #717 `card-terminal-explicit-stop` 分支之後的落地分支。
  - `state_consistency: 1`：只經既有 `_manager_update_workflow_run` 寫單一 run 的 bounded 欄位，外加既有的 reviewer sandbox 回收。
- 不改的檔案與函式：`terminalize_workflow_job`（verify／review 非通過的 raise 保留為守衛）、`_retryable_nonpassing_workflow_terminal`、`_malformed_workflow_card_terminal`、`_dispatch_workflow_card`、`manager_daemon.py`、`registry.py`、`work_actions.py`、`claim.py`、`diagnostics.py`、`terminal_contract.py`。
- 不做的事：
  - verify／review 壞形狀 terminal 的自動重派與熔斷（#578／#555）。
  - 把 review `status: failed` 轉成 GateEvaluation／`blocking-findings`。
  - 改 #750 `_prior_review_rejection` 的 review 欄位對應。
  - 改 `work show` 的 item 層投影。它由 `paulsha_cortex/monitor` 產出，`degraded` facet 來自 `monitor/lifecycle.py` 的 provider／correlation 診斷，與 run 的 needs_human 路徑無關；run 層 `next_actions` 非空由 spec R5／R8(f) 保證。
  - 改 operator resume 對 reviewer 卡的重派語意。
  - 處理 #803／#807 的成因。
  - 新增 CLI。
- spec／design／本 todo 的文字是 pinned authority：只准把 `[ ]` 翻成 `[x]`，其他文字一律不改；需要澄清就寫進 terminal reason。
- 只在 Manager 已 checkout 的分支上工作；不得建立任何 `wt/...` 分支。
- 不得 commit，也不得刪除 `docs/superpowers/plans/reviewer-honest-stop-terminal.md`。
- 測試與文件不得寫死 `openspec/changes/<change>/` 路徑。

## 現場證據

- paulshaclaw#296 job `wf-e3a0c5f0a9-verification-165`（agy）交出合法的 `workflow-verification-result` `needs_human`，Manager 卻落成 `terminalize-workflow-job-failed`／`ValueError: workflow verification terminal reported non-passing status: needs_human`，`evidence_refs: []`，summary／details 一字未落。
- paulshaclaw#341 的 `wf-ac3f66486a-verification-375`（claude，`failed`）落 `resume-workflow-failed`；同一個 run 的 `wf-ac3f66486a-code-review-390`（review，`failed`）撞 `workflow review terminal reported non-passing status: failed`。
- main `7fa4716b` 的 `manager.py`：
  - 5508 把明示停止入場判準寫死為 `{"plan", "build"}`。
  - 7356–7358、7411–7413 對 verify／review 非通過直接 raise。
  - 12110–12143 包成 `terminalize-workflow-job-failed` 後再 raise。
- `manager_daemon.py` 1242–1280：periodic 迴圈接住例外後覆寫成 `resume-workflow-failed`。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_reviewer_honest_stop_terminal.py`，斷言逐條對應 spec R8 (a)–(h)。
  - 樣板：review 用 `tests/test_review_authority_hashes_echo_922.py` 的 `_review_terminalize_fixture`／`_ResumeDispatcher`／periodic runner，複製進新檔，不 import 其他測試模組。verify 以同一樣板改成 `current_phase="verify"`、claim／define／plan／build step 標 passed、`verification` reviewer job。
  - fixture：verify 的 `summary` 與 `details.host_preflight_status`／`details.sandbox_limitations` 取 #874 本文引用的字串原樣（含本文裡的 `…`）；oracle 是 fixture 值原樣出現在 detail／`model_diagnostics`，不對照實機 log。review 用第二則留言的形狀，reason＋1 條 blocking finding。
  - verify `needs_human` 做 agy、claude 各一份：agy 份用 agy identity，log 為 `{"response": ...}` 單行 envelope，response 是「fenced 文字＋最後一行帶 `toolAction`／`toolSummary` 的 structured output」；claude 份用 claude identity，log 為 `{"type": "result", "result": ...}`。
  - 現行 main 必須 RED：(a)–(g) 會因 `ValueError: workflow ... reported non-passing status` 失敗。
- [x] **T2 source／形狀判準（R1、R2、R7、D1、D2）**：在 `manager.py` 新增 `_explicit_stop_gate_terminal(job) -> dict[str, object] | None`。
  - job 條件：`workflow_evidence is None`、exited、`type(exit_code) is int` 且為 0、phase ∈ {verify, review}。
  - log 用 `_extract_terminal_json` 解析，`ValueError` → `None`。
  - verify：key set 恰為六鍵、`schema_version` 是 int 1、kind 正確、status ∈ `NON_PASSING_STATUSES`、summary 非空字串、details 為 dict 或非空字串。
  - reports（verify／review 共用）：list，每個元素是鍵集恰為 `{"path", "body"}`、兩值皆為 strip 後非空字串的 dict；允許 `[]`；不跑 `_inline_terminal_reports` 的 manifest／root／大小檢查。
  - review：六鍵加可選 `authority_hashes`、reason 非空字串、findings 為 list 且每個元素是 dict；`authority_hashes` 內容與 finding schema 不驗。
  - phase／kind 錯配 → `None`。
  - 不動 `_retryable_nonpassing_workflow_terminal` 與它的三個 retry 消費點。
- [x] **T3 source／病因投影（R4、D3）**：新增 helper（建議名 `_gate_terminal_stop_diagnostics(job, raw)`），把 envelope 投影成 `TerminalDiagnostics`。
  - 有序 mapping：verify 為 `summary`→`details.<key>`（字串 details 為 `details`）；review 為 `reason`→`findings[<i>]`。
  - 把 mapping 包成 `{"diagnostics": mapping}` 交給既有 `_model_terminal_diagnostics`，沿用 2000 字全體預算與截斷標記。
  - `reason` 用 `workflow {label} terminal reported non-passing status: {status}`，`validation_path="$.status"`，`observed_head` 取法與 `_terminal_parse_diagnostics` 相同。
- [x] **T4 source／落地分支（R3、R5、R6、D4、D5、D6）**：在 `resume_workflow_run` 的 #717 分支之後、`_malformed_workflow_card_terminal` 之前加分支，依序：
  - 判準命中後組 diagnostics。
  - `_discard_reviewer_sandbox(..., require_candidate_unchanged=True)`；`ValueError` → `reviewer-candidate-drift`（detail 帶 declared status 與例外摘要）。
  - 否則以 `_manager_update_workflow_run` 加 `needs_human` facet（保留既有 facets）、`gate_status="running"`，寫入 `diagnostic_reason("verification-terminal-explicit-stop"|"review-terminal-explicit-stop", f"{label} terminal 明示要求停止（status={declared}）：{model_text}", source="manager._poll_workflow_job:explicit-stop", evidence_refs=(log_path,), run_id=..., work_id=..., job_id=..., card=..., declared_status=..., phase=...)`。
  - 回傳 `{run_id, current_phase, job_id, reason, declared_status, terminal_diagnostics}`。
  - 不呼叫 `terminalize_workflow_job`、不 raise、不改 `attempts`、不派 job、不綁 evidence。
- [x] **T5 tests／回歸（R7）**：以下既有測試全綠、斷言不改：`tests/test_terminal_diagnostics_717.py`、`tests/test_terminal_result_contract.py`、`tests/test_agy_reviewer_json_schema_880.py`、`tests/test_review_authority_hashes_echo_922.py`、`tests/test_workflow_production_wiring.py`、`tests/test_retry_verify_job_invalidation.py`、`tests/test_manager_daemon_tick_isolation.py`、`tests/test_provider_failure_recovery.py`、`tests/test_work_actions.py`。
  - 另補兩條斷言：「verify／review envelope 對 `_retryable_nonpassing_workflow_terminal` 仍為 `False`」、「plan／build job 對新 predicate 為 `None`」。
  - 最後跑 `python3 -m pytest -q` 全套確認。
- [x] **T6 documentation／changelog／CLI help**：
  - 新增 `changelog.d/reviewer-honest-stop-terminal.md`，並同步 `CHANGELOG.md [Unreleased]` 一則 entry（#874）。
  - `docs/unified-work-lifecycle.md` 的「Terminal/result contract（#261）」段補一段：verify／review 形狀合法的 `failed|needs_human` terminal 落 `verification-terminal-explicit-stop`／`review-terminal-explicit-stop`（needs_human、帶原文與 job log evidence_ref、不自動重派、重派出口是 `retry-card`／`retry-build`）。
  - `README.md` 中「plan/build workflow card以schema/binding正確的terminal明示`failed|needs_human`」那句補上 verify／review 的對應行為：一樣轉 `needs_human`、periodic runner 不重派；但 explicit resume 只會重落同一個停止、不重派，重派出口是 `retry-card`／`retry-build`（與 plan／build 的「explicit resume 重試同一 run/card」不同，見 design D6）。
  - 本票不新增 CLI：以 `python3 -m paulsha_cortex.cli work --help`（或 `cortex work --help`）做 help smoke，確認輸出不變。
