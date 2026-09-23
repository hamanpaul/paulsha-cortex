---
status: accepted
work_item: review-gate-adjudication-exit
domain_breadth: 1
state_consistency: 1
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Review gate `blocking-findings` 的 operator 裁決出口（#956）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#956`；[spec](../../specs/review-gate-adjudication-exit-spec.md)、[design](../../specs/review-gate-adjudication-exit-design.md)。
- 觸及模組（3 個 production 模組 → `domain_breadth: 1`）：`paulsha_cortex/coordinator/work_actions.py`（`_retry_review_action`、`execute_work_action` retry-review 分支、`_phase_recovery_actions`、新增 `_blocking_findings_recovery_actions`／`blocking_findings_next_step_hint`、`_claim_action` needs_human 回應、`_review_attest_action`）、`paulsha_cortex/coordinator/manager.py`（`OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE`、`workflow_status_entry`）、`paulsha_cortex/coordinator/cli.py`（只改 `--reason` help）。`state_consistency: 1`：`retry-review` 在既有 registry reset 後多寫一筆 immutable content-addressed operator-adjudication evidence，不新增 registry 欄位。
- 明確不做：不改 review gate 判準（`review.py` 的 `BLOCKING_FINDING_CATEGORIES`／`_normalize_finding`／`validate_review_verdict`、`manager.apply_workflow_action` rejected 分支）；不依 severity／recommendation 改判（#956 建議 2）；不讓 `review-attest` 成為 review 卡 gate 的採信來源（#956 建議 3）；不改 `work_bridge.py` ship maintainer 路徑、`retry-card` accepted-evidence 拒絕、`registry.py` 三支 reset、`control/contract.py`、`manager._operator_adjudications` 的讀取上限（每筆 reason 截 2000 字、最多 3 筆；調高會同時放大三個寫入端的 prompt，evidence 仍保留 ≤4000 字全文）、`claim.needs_human_next_actions`、porcelain `run.py`／`recover.py`、`paulsha_cortex/cli.py` 的 `_WORK_HELP`、slice lane；不處理 #935（ship 段 needs-fix disposition）、#953（operator ruling 生命週期）、#546（recovery helper 抽共用、`regenerate-gates` 曝光面）。
- spec／design／本 todo 文字是 pinned authority，只准 `[ ]`→`[x]` 翻 checkbox；澄清寫進 terminal reason。
- 留在 Manager checkout 的分支上工作，不得另開 `wt/...` 分支。
- 不得 commit 或刪除 `docs/superpowers/plans/review-gate-adjudication-exit.md`。
- 測試與文件不得寫死 `openspec/changes/<change>/` 路徑。

## 現場證據

- 2026-09-23 pin `442fe23f`、#948 run `workflow-5fb6da3bc6d2cfc3c3ed`：兩輪 code-review `status: passed` 各帶一條 minor finding（`scope-bypass`，reviewer 自寫「confirm with the operator」），review gate 判 `rejected` → `blocking-findings`；`review-attest`→`resume` 成功但 phase 不動、`retry-card` → `refuses a card with accepted evidence`、`retry-review --reason` → `rejects caller evidence/input: reason`、`next_actions` 只有 `abandon`；以場外 merge PR #952＋`retire-delivered` 收尾。
- main `7fa4716b`：`work_actions.py` `_retry_review_action` allowlist（2875-2879）無 `reason`、`execute_work_action`（6401-6406）不傳 `state_path`／`now_epoch`；`_phase_recovery_actions`（2522-2599）不處理 blocking-findings；`_review_attest_action`（1277-1380）不看 review step `gate_result`，並在 1378 清掉 `needs_human`。
- main `7fa4716b`：`manager.py` 12678 把 rejected 卡 `gate_result` 寫成 `needs_human`、12681-12713 落 `blocking-findings`；`_operator_adjudications`（9981-10027）與 11249 dispatch 注入已是 run 級；`OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE`（9789-9794）無「已接受 finding 不得再以 blocking 回報」；`review.py:785` 任一 blocking 類別即 rejected；`coordinator/cli.py:232-238` `--reason` help 只列 abandon 族。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_review_gate_adjudication_exit.py`，逐條對應 spec R9 (a)–(j)；review-phase run 沿用 `tests/test_work_actions_retry_invalidation.py` 的 `_authority`／`_step`／`_base_steps` 樣板並自建 factory 帶 `needs_human_reason=fixture_needs_human_reason("blocking-findings", ...)` 與 plan authority，resume／attention 曝光沿用 `tests/test_reviewer_card_retry_569.py` 寫法，review-attest 沿用 `tests/test_work_actions.py::test_review_attest_writes_immutable_exact_head_evidence` 的 fake GitHub 樣板；現行 main 必須 RED（retry-review 拒收 reason、`_phase_recovery_actions` 無 retry-review／retry-build、review-attest 對 rejected review 成功寫 evidence、directive 缺類別名、help 無 `4000`）。
- [ ] **T2 source／retry-review 接受 reason（R1、R2、D1、D2、D3）**：`_retry_review_action` 加 keyword `state_path`／`now_epoch`，allowlist 加 `reason`，allowlist 檢查後立即 `_validate_operator_adjudication_args(action="retry-review")`；reset 前以 lazy import `_current_workflow_step` 決定 card（D2 fallback：最後一張 review step → `"review"`）；reset 與 `_recompute_and_persist_sizing` 成功後 `_record_operator_adjudication`，回傳補 `adjudication_evidence`／`adjudication`（無 reason 為 `None`）；`execute_work_action` retry-review 分支傳 `resolved_state_path`／`now_epoch`；reset 失敗不寫 evidence。
- [ ] **T3 source／reviewer directive（R3、R4、D5）**：`OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE` 既有字串後串接 D5 的一句，blocking／non-blocking 類別清單由 `foreign_review.BLOCKING_FINDING_CATEGORIES`／`VALID_FINDING_CATEGORIES` 排序導出；`OPERATOR_ADJUDICATION_DIRECTIVE`、`_OPERATOR_ADJUDICATION_PREAMBLE`、`_operator_adjudications`、`_workflow_job_prompt`、dispatch 接線不改。
- [ ] **T4 source／next_actions 與 next_step_hint（R5、R6、D6、D7）**：`_phase_recovery_actions` 把 `reason_code` 提到開頭，既有 `list_jobs()` try/except 補 `jobs_readable` 旗標，於 no-active-job 分支內 retry-card 判定之後、且僅在 `jobs_readable` 為真時呼叫 `_blocking_findings_recovery_actions(run)`（前置逐條對照 spec R5，順序 retry-review→retry-build；`list_jobs()` 失敗 → 兩者都不宣告，既有 regenerate-gates／retry-card 行為不變）；新增純函式 `blocking_findings_next_step_hint(*, work_id, repo, candidate)`（格式不符用佔位）；`_claim_action` needs_human 補充區塊在 extras 含 `retry-review` 且 reason 為 `blocking-findings` 時覆寫 `next_step_hint`；`manager.workflow_status_entry` 合併 extras 後，無 persisted hint、reason 為 blocking-findings 且 `next_actions` 含 retry-review 時改用新 hint（import 併入既有 `try`）。
- [ ] **T5 source／review-attest 拒絕 rejected review gate（R7、D8）**：`_review_attest_action` 在 `_validate_current_run_authority` 之後、`foreign` 前置檢查與 GitHub 讀取之前，任一 review step `gate_result == "needs_human"` → `RuntimeError`（訊息含 `review-attest does not adjudicate review-gate blocking findings`，並指向 `retry-review --reason`／`retry-build --reason`）；不寫 maintainer evidence、不改 `gate_refs`／`facets`。
- [ ] **T6 tests／回歸**：`tests/test_work_actions_retry_invalidation.py`、`tests/test_wiring_retry_sizing_recompute.py`、`tests/test_operator_adjudication_752.py`、`tests/test_adjudication_builder_prompt_814.py`、`tests/test_reviewer_card_retry_569.py`、`tests/test_midchain_builder_retry_545.py`、`tests/test_work_actions.py`、`tests/test_copilot_review_adopt_existing.py`、`tests/test_workflow_production_wiring.py`、`tests/test_coordinator_cli_flags.py`、`tests/test_work_cli.py` 全綠、不改斷言；再跑 `python3 -m pytest tests/ -q` 全綠。
- [ ] **T7 documentation／changelog／CLI help（R8、D9）**：`paulsha_cortex/coordinator/cli.py` 的 `work --reason` help 依 spec R8 改成兩組（retry-build／retry-card／retry-review 裁決最多 4000 字，evidence 存全文、前 2000 字注入 prompt；abandon 族單行審計理由最多 500 字），help smoke：`python3 -m paulsha_cortex.cli work retry-review --help` exit 0 且含 `4000`、`python3 -m paulsha_cortex.cli work --help` 輸出不變；新增 `changelog.d/review-gate-adjudication-exit.md` 並同步 `CHANGELOG.md [Unreleased]`；`docs/unified-work-lifecycle.md` 的「operator 裁決（`--reason`）」段補 `retry-review`，並註明每筆裁決進 prompt 時只帶前 2000 字（evidence 存全文），「needs_human 的 next_actions」段補一句「`blocking-findings` 宣告 `retry-review`（接受）／`retry-build`（駁回）並附指令提示；`review-attest` 對 rejected review gate fail closed」。
