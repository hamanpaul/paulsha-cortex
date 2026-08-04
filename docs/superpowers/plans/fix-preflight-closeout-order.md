---
status: accepted
work_item: fix-preflight-closeout-order
---

# fix-preflight-closeout-order Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_preflight_closeout_order.py`（fixture 風格比照 `tests/test_work_bridge.py`：`JobRegistry(state_path=tmp_path / "jobs.json")` + tmp_path 真實 git worktree + spy runner 記錄 argv 序列），先寫以下測試並確認全部失敗：
  - `test_ship_validate_completes_local_archive_closeout_without_pr_binding`：run 為 review-complete、`pr_refs` 為空、builder worktree 內 active openspec change 目錄存在；validate 完成 local closeout（官方 archive、archive commit 產生新 candidate、run reset 回 verify），spy runner 全程未收到 `gh` 或 `git push` argv。
  - `test_pr_metadata_preflight_failure_keeps_local_closeout_and_resumable`：archive 已完成（active change 目錄不存在），preflight runner 對 metadata 模式回非 0；validate 回 trusted `needs_human` 結果（reason 含 `pr-preflight-blocked`），resume 後 run 帶 `needs_human` facet 但 `gate_status` 不是 `"failed"`，再次 resume 可重試 preflight，不需 registry surgery。
  - `test_stage_order_closeout_then_preflight_then_ship`：spy runner 斷言 argv 順序——archive／本地 git 操作先於任何 preflight argv，preflight argv 先於任何 `gh` mutation argv。
  - `test_no_external_mutation_without_authorization`：pre-PR 全流程（closeout 完成、停在 PR-binding 邊界）中 runner 收到的 argv 不含 `gh` 與 `git push`，delivery journal 內容零變更。
  - `test_archive_commit_does_not_push`：`_commit_archive_and_require_reverification` 完成後 runner 未收到 push argv，run 的 `candidate_head` 已更新為 archive 後新 SHA。
  - `test_reviewer_dispatch_fail_closed_on_frozen_hash_drift`：materialize 後竄改 workspace 內 authority 檔內容，延伸後的 `verify_authority_in_input_snapshot(workspace_root=...)` raise hash drift，reviewer 不 dispatch。
  - `test_review_worktree_materializes_frozen_authority_with_attestation`：slice-based `prepare_review_worktree` 路徑 materialize frozen refs 後，workspace 內每個 authority ref 實檔 sha256 等於 baseline，且 materialization 紀錄含相對路徑、sha256、source revision 與 candidate SHA；缺 authority 參數時 fail-closed。
  - `test_unexpected_exception_still_fails_closed`：validate 拋非 typed 例外時，ship advance 仍設 `needs_human` + `gate_status="failed"`（既有 fail-closed 行為不回歸）。
- [ ] 驗收：`python3 -m pytest tests/test_preflight_closeout_order.py -q` 顯示上述測試全部 FAIL（RED）。

### 2. workflow.py ship transition 子階段常數

- [ ] `paulsha_cortex/coordinator/workflow.py`：緊鄰 `WORKFLOW_PHASES`（line 14）新增 `SHIP_TRANSITION_STAGES = ("local-closeout", "pr-preflight", "external-ship")` 與 `validate_ship_stage_transition(current, new)`（比照 line 701 `validate_workflow_phase_transition` 的單調前進驗證；非法值與倒退一律 ValueError）。不改 `WORKFLOW_PHASES`、`WorkflowRun` 欄位與 ship invariants（line 534-557）。
- [ ] 驗收：新函式的合法前進／倒退／非法值測試通過；`python3 -m pytest tests/ -q -k "workflow"` 全綠。

### 3. ship validator 三段重排（local closeout 前移）

- [ ] `paulsha_cortex/coordinator/work_bridge.py`：`build_production_ship_validator` 的 `validate`（line 1378）重排——在 `_builder_binding`／`_remove_canonical_untracked_reports`（line 1403-1415）之後、`_pr_metadata`（line 1416）之前插入 local closeout 段：builder worktree 的 `openspec/changes/<change>` 目錄存在時，呼叫 `work_actions._validate_local_archive_inputs`（`work_actions.py:108`）→ 執行官方 archive argv → `_commit_archive_and_require_reverification`（line 783）→ 回傳 `candidate-reverification-required` trusted 結果。change slug 取自 `load_work_authority` 的 `mapped_openspec`（validate 內已有 authority，line 1395-1399），不依賴 `_ship_binding` 的 `pr_number`。
- [ ] `_pr_metadata`（line 1416）、初次 preflight（line 1427-1437）、push（line 1438）、PR 建立（line 1449）、既有 PR preflight＋push（line 1487-1524）與 `_ship_action`（line 1567）全部保持在 local closeout 段之後執行；`_ship_action` 內的 archive 段（`work_actions.py:2904-2924`）與 remote archive 檢查（`work_actions.py:2955-2956`）原樣保留。
- [ ] 驗收：task 1 的 `test_ship_validate_completes_local_archive_closeout_without_pr_binding` 與 `test_stage_order_closeout_then_preflight_then_ship` 全綠。

### 4. archive commit 去除內嵌 push

- [ ] `paulsha_cortex/coordinator/work_bridge.py`：`_commit_archive_and_require_reverification`（line 783）移除 `_push_exact_candidate` 呼叫（line 841-850）；`_record_manager_ship_job`（line 851）與 `_manager_reset_workflow_after_archive`（line 861）保留。archive 後 candidate 的 push 由 external 段既有路徑（line 1438、line 1514）在下一輪 ship 承擔。
- [ ] 驗收：task 1 的 `test_archive_commit_does_not_push`、`test_no_external_mutation_without_authorization` 全綠；`python3 -m pytest tests/test_work_bridge.py -q` 全綠（既有行為不回歸）。

### 5. preflight typed stop 與 resume 語意

- [ ] `paulsha_cortex/coordinator/work_bridge.py`：line 1437（`initial PR-metadata preflight failed`）與 line 1512（`existing PR exact-Candidate preflight failed`）改為：以 `_write_json_evidence` 落地 preflight 失敗 evidence（含 failed_stage、policy／ci-parity returncode、head、tree_hash）後，回傳 `{"trusted": True, "status": "needs_human", "reason": "pr-preflight-blocked", "head": candidate, "commit_id": candidate, **evidence}`——`validate_ship_result`（`manager.py:6676-6702`）已接受 `needs_human`，不放寬其驗證。
- [ ] `paulsha_cortex/coordinator/manager.py`：ship advance 例外 handler（line 6467-6473）原封不動（意外例外仍 `gate_status="failed"`）；確認 typed stop 走正常回傳路徑後 run 的 facets／gate_status 呈現「可 resume」狀態，resume 回傳 reason 帶下一個合法 operator action（`awaiting-pr-authorization` 語彙）。
- [ ] 驗收：task 1 的 `test_pr_metadata_preflight_failure_keeps_local_closeout_and_resumable` 與 `test_unexpected_exception_still_fails_closed` 全綠；`python3 -m pytest tests/test_delivery_preflight.py tests/test_delivery_orchestrator.py tests/test_delivery_final_before_merge.py -q` 全綠。

### 6. review materialize hash 驗證延伸

- [ ] `paulsha_cortex/coordinator/review.py`：`verify_authority_in_input_snapshot`（line 261-292）新增可選 keyword 參數 `workspace_root: str | Path | None = None`——提供時逐 authority ref 重讀 `workspace_root / ref` 實檔、重算 sha256 比對 frozen baseline，缺檔沿用 `review input snapshot missing frozen authority`、不符沿用 `review input snapshot authority hash drift` 錯誤語彙；未提供時行為與現況完全一致（既有呼叫端 `manager.py:6056` 不受影響）。
- [ ] `paulsha_cortex/coordinator/review.py`：`prepare_review_worktree`（line 226）擴充可選參數（authority mapping＋input snapshot rows）：worktree checkout 驗證 HEAD 後，比照 `manager.py:4010-4031` 的 seed 寫法 materialize frozen refs，再以 `workspace_root=worktree` 呼叫延伸後的驗證；`manager.py:924` 呼叫端補傳參數，materialization 紀錄（相對路徑、sha256、source revision、candidate SHA）經 evidence 流落地，不新增 job row 欄位。
- [ ] 驗收：task 1 的兩條 review materialize 測試全綠；`python3 -m pytest tests/test_coordinator_foreign_review.py tests/test_review_reviewer_attestation.py -q` 全綠。

### 7. operator 文件

- [ ] `docs/unified-work-lifecycle.md`：ship 段補「local-closeout → pr-preflight → external-ship」三段語意：pre-PR 可完成的本地 closeout 範圍、`pr-preflight-blocked`／`awaiting-pr-authorization` 停止點與下一步合法 operator action、frozen materialize hash 驗證與 fail-closed 行為、與 #275 的邊界（terminal transition 屬 W3 另案）。
- [ ] 驗收：段落與 spec R1–R6 敘述一致；`python3 -m policy_check` 的 doc 相關規則無新增 FAIL。

### 8. 交付要件

- [ ] `changelog.d/fix-preflight-closeout-order.md` fragment 已新增且已 commit（R-09 硬性 gate，只 add 不 commit 仍 FAIL）。
- [ ] `CHANGELOG.md [Unreleased]` 對應 entry（Refs #263）。
- [ ] cortex CLI help 同步確認（R-16；本票未新增 CLI 動作，確認 `cortex work`／`cortex recover work` help 無需變更並記錄於 PR body）。
- [ ] 帶 PR 上下文執行 policy_check 確認 0 fail：`python3 -m policy_check --repo . --pr-title "..." --pr-body "..." --pr-labels "..." --pr-base-ref main --pr-head-ref "feature/263-fix-preflight-closeout-order"`。
- [ ] `python3 -m pytest tests/ -q` 全綠。
