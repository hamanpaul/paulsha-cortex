---
status: accepted
work_item: retry-build-preserve-proof
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# retry-build 在 launch 前失敗時保留 slice 既有 proof（#479 殘項）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#479`（殘項）；[spec](../../specs/retry-build-preserve-proof-spec.md)、[design](../../specs/retry-build-preserve-proof-design.md)。票面主缺陷（retry-build 沒傳 `launcher_factory`）已由 `6b4160f6`（PR #941）修掉，本項不重做、不改 `manager_daemon.py`／`cli.py`／`porcelain/recover.py`。
- 觸及模組（2 個 production 模組 → `domain_breadth: 1`）：`paulsha_cortex/coordinator/autonomy.py`（`dispatch_ready` 恢復態快照、`launched` 旗標、except 分支，新增 `_recovery_slice_snapshot`／`_restore_recovery_slice`／`_settle_recovery_dispatch_failure`）、`paulsha_cortex/coordinator/manager.py`（`complete_tick` build 分支的 `_is_unbound_launch_failed_build_job` skip）。`state_consistency: 1`：只經既有已驗證 writer 寫回單一 slice row 的既有欄位，不新增 durable 欄位、無跨物件 CAS。
- 明確不做：不改 `registry.py`（不新增 writer；#862／#497 的 receipt／revision／supersession 範圍）；不處理一般 superseded terminal job 重播與 manifest ping-pong（#481／#497）、`recover-pre-candidate` 清綁定（#497）、retry-build 對既有 candidate worktree／branch 的 provision 語意（`GitWorktreeCreator.create` 的拒絕本身）、verified slice 退回（#502）、needs_human 無 manifest 時 tick 重複派工、worktree／job row 回收、還原中途崩潰的原子性；不改 `apply_slice_action` 回傳形狀、不新增 CLI。
- spec／design／本 todo 文字是 pinned authority，只准把 `[ ]` 翻成 `[x]`，不得改寫其他文字；澄清寫進 terminal reason。
- 留在 Manager 已 checkout 的分支上工作，不得建立任何 `wt/...` 分支。
- 不得 commit 或刪除 `docs/superpowers/plans/retry-build-preserve-proof.md`。
- tests 與 docs 不得寫死 `openspec/changes/<change>/` 路徑。

## 現場證據

- 2026-08-12 #479 五則 recurrence（review-finding 修復迴圈）：`slice-action ... retry-build` 失敗後 `builder_job_id`／`reviewer_job_id`／`candidate` 被清空、spec 被 repin 成 recovery-context hash、`dispatch_base` 變前一個 candidate、reason 變 `missing-slice-proof`，只剩 `recover-pre-candidate`／`abandon`，candidate 要 operator 另存 archive ref。
- 現行 main `7fa4716b`：`dispatch_ready` 在 prompt／base_sha／worktree 之前就 repin（`autonomy.py:740-746` → `:1105` → `registry.py:1555-1564`）；except 對 repin 前的失敗也補 repin（`autonomy.py:825-834`）；`_mark_slice_needs_human`（`:843-844`）把 `failed` 改成 `needs_human`；`allowed_slice_actions` 對無合法 candidate 的 needs_human 只給 `recover-pre-candidate`／`abandon`（`manager.py:668-669`）。
- `GitWorktreeCreator.create` 對帶 candidate commit 的 `feature/<slice>` 拋 `existing worktree branch has commits outside requested base`（`seams.py:224`）並自行 rollback——帶 candidate 的 retry-build 最常見的 launch 前失敗；`launch()` 拋錯時 `_fail_launching_job` 寫 `runtime_diagnostic.reason = "launch-failed"`（`autonomy.py:1239-1244`），`complete_tick` 的 manifest 冪等只比單槽 `job_id`（`manager.py:2468`）。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_retry_build_preserve_proof.py`，逐列對應 design D7 矩陣（worktree 失敗且 retry 前改寫 spec 檔、`launcher_factory=None` 與 unknown identity、spawn 失敗＋`complete_tick` 兩次、failed 恢復態、control request 路徑、退路三例、修正後重試、行為不變）；沿 `tests/test_coordinator_operator_actions.py`、`tests/test_fix_slice_failed_deadend.py`、`tests/test_executor_backoff_slice_lane.py` 既有 fixture 樣板。現行必須 RED：worktree 失敗／`launcher_factory=None`／spawn 失敗三列的 `candidate`、`builder_job_id`、`reviewer_job_id`、current refs 被清，worktree 失敗列的 `spec.hash` 被改成改寫後的 hash，failed 恢復態被改成 `needs_human`。
- [x] **T2 source／快照與 launch 界線（R1、R5、D1–D3）**：`autonomy.py` 新增 `_recovery_slice_snapshot`（deep copy、永不拋錯、只對 `state ∈ {needs_human, failed}` 回 dict）並在每個就緒單位迭代的第一步呼叫；per-slice 加 `launched`（於 `active_launcher.launch(...)` 回傳後設 True）與 `written_dispatch_base`（記本次迭代最後寫入 slice 的 `dispatch_base`）；except 分支的 `_record_pending_slice` 補呼叫加 `snapshot is None` 條件；`snapshot is None` 或 `launched` 為 True 的路徑逐字維持現行。
- [x] **T3 source／還原與收尾（R1、R2、R4、D4、D5）**：新增 `_restore_recovery_slice`（重讀＋R4 前置條件：pins／`dispatch_base`／綁定／candidate／refs／evidence hash／`gate_state`／`state` 皆為本次迭代寫入後的形狀，且 `actions`／`evidence_history`／`evaluation_history` 長度等於快照 → 必要時 `update_slice(needs_human)` → `repin_slice(prior pins)` → `update_slice(prior state／gate／綁定／candidate／refs／evidence hash)`）與 `_settle_recovery_dispatch_failure`（已還原或未 repin → best-effort `record_action(action="dispatch-failed", actor="manager")`，拋錯只記 log；還原前置條件不成立或還原寫入拋錯 → `logger.warning` 後走 `_mark_slice_needs_human`）；恢復態不再呼叫 `_mark_slice_needs_human`；`errors`／`DispatchReadyError` 不變；不新增 registry 方法。
- [x] **T4 source／complete_tick audit-only skip（R3、D6）**：`manager.py` 新增 `_is_unbound_launch_failed_build_job(registry, slice_id, job)`（`status == "failed"`、非 workflow lane、`runtime_diagnostic.reason == "launch-failed"`、slice row 存在且 `builder_job_id != job_id`），在 `complete_tick` build 分支 `_slice_for_job` 與 reviewer skip 之後、`_repo_root_for_slice_row` 之前 `continue`（不論 launch-failed 來自 spawn、attach 或 handle 缺失，只看是否已非 owner）；review kind、workflow lane、仍綁定的 launch-failed job、一般 superseded job 行為不變。
- [x] **T5 tests／回歸（R5、R6）**：`tests/test_coordinator_operator_actions.py`、`tests/test_executor_backoff_slice_lane.py`、`tests/test_slice_executor_model.py`、`tests/test_pinned_spec_delivery_503.py`、`tests/test_fix_slice_failed_deadend.py`、`tests/test_persona_phase4_fanout_autonomy.py`、`tests/test_pre_candidate_recovery.py`、`tests/test_coordinator_dispatch_discipline_e2e.py` 全綠、不改既有斷言；全套 `python3 -m pytest -q` 無新失敗；補「首次派工 spawn 失敗的綁定 launch-failed job 仍落 `failed`／`builder-failed-*`」「backoff skip 仍無 slice／job 寫入」斷言。
- [x] **T6 documentation／changelog／CLI help**：新增 `changelog.d/retry-build-preserve-proof.md` 並同步 `CHANGELOG.md [Unreleased]`（說明 #479 殘項：retry-build launch 前失敗保留 candidate／evidence／綁定與 state、未 launch 的 attempt 在 `complete_tick` 只供稽核、主缺陷已由 #941 修）；README「Operator actions 與 status / attention」段補一條：`slice-action retry-build` 在 launch 前失敗會回報錯誤並保留 slice 既有 candidate、verification／review refs、builder／reviewer 綁定與 state，修正原因後可直接再次 `retry-build`；本票不新增 CLI，以 `python3 -m paulsha_cortex.cli slice-action --help` help smoke 驗證輸出不變。
