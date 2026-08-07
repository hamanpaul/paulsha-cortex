---
status: accepted
work_item: fix-repair-commit-recovery
---

# fix-repair-commit-recovery Plan

## Tasks

### 1. TDD RED

- [x] 新增 `tests/test_repair_commit_recovery.py`（fixture 風格比照 `tests/test_pre_candidate_recovery.py`：`JobRegistry(state_path=tmp_path / "jobs.json")` + 真實 git worktree in tmp_path），先寫以下測試並確認全部失敗：
  - `test_recover_repair_commit_adopts_descendant_head_without_terminal_evidence`：build phase、final builder card pending、最新 builder job `status="failed"`、worktree HEAD 為原 candidate 的 descendant commit；執行 `recover-repair-commit` 後 `candidate_head` 更新為該 SHA、run 進 verify phase、產生 `cortex-work-repair-adoption/v1` evidence record 與 exited/0 adoption job row。
  - `test_recover_repair_commit_rejects_non_descendant_head`：worktree HEAD 不是原 candidate 的 descendant 時 fail closed，run 狀態不變。
  - `test_recover_repair_commit_rejects_dirty_worktree`：worktree 有未 commit 變更時 fail closed，run 狀態不變。
  - `test_recover_repair_commit_fail_closed_when_terminal_evidence_bound`：最新 builder job 已有 `workflow_evidence` 時拒絕（不得成為繞過 terminalization 的後門）。
  - `test_recover_repair_commit_replay_already_recovered_no_new_job`：adoption 成功後重送相同 request 回 `already-recovered`，job 總數與 evidence record 數不變。
  - `test_recover_repair_commit_rejects_unauthorized_issue`：`--issue` 不在 WorkAuthority mapped issues 時拒絕。
  - `test_resume_first_call_dispatches_replacement_not_stale_failed_job`：最新同 card job 為 `status="exited"`、exit code 非 0 時，第一次 operator resume 即 dispatch replacement job，不回報 stale job 的 `job-failed`。
  - `test_resume_replay_keeps_single_replacement_job`：replacement dispatch 後再次 resume 回報 in-flight，不產生第二個 replacement job。
  - `test_retry_build_expected_candidate_cas_unchanged`：`retry-build` 缺 `expected_candidate` 或 SHA 不符 `run.candidate_head` 時，錯誤訊息與現況一致（`retry-build requires exact expected_candidate`／`retry-build expected Candidate CAS mismatch`），確認 CAS 未被放寬。
- [x] 驗收：`python3 -m pytest tests/test_repair_commit_recovery.py -q` 顯示上述測試全部 FAIL（RED）。

### 2. registry 原子 adoption 方法

- [x] `paulsha_cortex/coordinator/registry.py`：新增 `_manager_adopt_repair_candidate(run_id, *, expected_candidate, adopted_candidate, adoption_job_id, evidence_ref)`，緊鄰 `_manager_reset_workflow_for_retry_build` 並沿用其 CAS／admission 風格：ongoing + `needs_human` + `current_phase == "build"`、`candidate_head == expected_candidate`、前置 build steps 全 passed、final build card pending、無 `ACTIVE_JOB_STATUSES` job。單一 persist 內完成：`candidate_head=adopted_candidate`、final build step 以 adoption job 的 executor/model/independence_domain 標 passed、`current_phase="verify"`、`attempts["verify"]` 遞增、移除 `needs_human` facet、`verified_head=None`、evidence_refs 附掛 `evidence_ref`。
- [x] 驗收：task 1 的 adopts 測試中對 run 欄位的斷言通過；不新增任何 WorkflowRun／job row 欄位名。

### 3. work action 本體

- [x] `paulsha_cortex/coordinator/work_actions.py`：新增 `_recover_repair_commit_action`（緊鄰 `_recover_pre_candidate_action`），參數白名單 `{"action","repo","work_id","issue","actor","expected_run_id","expected_candidate"}`，其餘一律 `rejects caller evidence/input`。流程：驗 `expected_run_id`（`workflow-[0-9a-f]{20}`）與 `expected_candidate`（`verification.SAFE_SHA_RE`）→ 冪等判定（先於其餘 admission）→ issue 授權檢查與 WorkAuthority refs 一致性 → 以 run 事實找出 final build card 最新 builder job（終止且 `workflow_evidence is None`）→ 取其 worktree 做 git 驗證（HEAD 精確等於 expected SHA、`git status --porcelain` 空、`merge-base --is-ancestor` 原 candidate 為祖先且兩者不等）→ 寫 `cortex-work-repair-adoption/v1` record（`evidence/work-repair-adoption/<run_id>-<digest>.json`，比照 `_abandon_record` 的 digest 命名；body 含 failed job id、observed HEAD、adopted/previous candidate、actor）→ 登錄 adoption job row（identity/worktree/dispatch_head 複製自 failed job、`subject_head=adopted SHA`、exited/0、`workflow_evidence` 指向 record；不新增欄位）→ 呼叫 `_manager_adopt_repair_candidate`。冪等分支：`candidate_head` 已等於 expected SHA 且 record 存在 → 回 `already-recovered`。
- [x] `execute_work_action` 的 action 白名單與 dispatch 分支加入 `"recover-repair-commit"`。
- [x] 驗收：task 1 的六條 `recover_repair_commit` 測試全綠。

### 4. resume／dispatch stale failed job 選擇修正

- [x] `paulsha_cortex/coordinator/manager.py`：新增 `_is_stale_terminalized_failed_job()` 把「`status == "exited"` 且 `exit_code` 為非 0 int」併入既有 `status == "failed"` 判定，套用到 `resume_workflow_run` 的 replacement 判定與 `_dispatch_workflow_card` 的 `retryable_latest` 判定兩處；exited/0 的既有路徑（unbound terminal、malformed schema retry、正常 terminalize）條件式不動。
- [x] resume 對失敗 job 的 `job-failed`／`reviewer-candidate-drift` 回報附掛 `_terminal_parse_diagnostics(job).as_dict()`（唯讀診斷，不授予 authority）。
- [x] 驗收：task 1 的兩條 resume 測試全綠；`python3 -m pytest tests/test_work_actions.py tests/test_retry_classification.py tests/test_work_actions_retry_invalidation.py tests/test_terminal_result_contract.py tests/test_retry_verify_job_invalidation.py -q` 全綠（既有行為不回歸）。

### 5. control queue 契約

- [x] `paulsha_cortex/control/contract.py`：`WORK_ACTIONS` frozenset 加入 `"recover-repair-commit"`；`validate_request` 比照 `recover-planning` 分支新增驗證——`expected_run_id` 須 `workflow-[0-9a-f]{20}`、`expected_candidate` 須 40-hex，缺漏或格式錯誤時拋 `ValueError`。
- [x] 驗收：`python3 -m pytest tests/test_control_client.py tests/test_control_contract.py -q` 全綠。

### 6. CLI 與 porcelain 面（R-16）

- [x] `paulsha_cortex/cli.py`：`cortex work` usage 行與動作說明清單加入 `recover-repair-commit  對 repair commit 已存在但缺 terminal evidence 的 build 失敗做具 CAS 的採納恢復`。
- [x] `paulsha_cortex/coordinator/cli.py`：work action choices 加入 `"recover-repair-commit"`。
- [x] `paulsha_cortex/porcelain/recover.py`：`recover work` 的 action choices 加入 `"recover-repair-commit"`（`--expected-candidate`／`--expected-run-id` 既有 flags 直接沿用，`_work_args` 無需改動）。
- [x] 驗收：`cortex work` 與 `cortex recover work` 的 `--help`／usage 輸出含新動作；`python3 -m pytest tests/test_work_cli.py tests/test_porcelain_recover.py -q` 全綠。

### 7. operator 文件

- [x] `docs/unified-work-lifecycle.md`：在既有 recovery 敘述段（retry-build 窄化入口與 recover-planning／recover-pre-candidate 說明附近）補一段 `recover-repair-commit`：適用情境（failed repair job 留下 descendant commit）、雙 CAS 參數、判準全取自系統事實、冪等語意、adoption 後由 verify → foreign review → exact-head final 重新把關、periodic runner 不取得此 authority；並補一段 resume/retry-build stale failed job 選擇修正的敘述。
- [x] 驗收：段落存在且與 spec R1–R5 敘述一致；`python3 -m policy_check` 的 doc 相關規則無新增 FAIL（R-22 僅既有 32 筆 pre-existing dangling reference，與本次變更無關）。

### 8. 交付要件

- [x] `changelog.d/fix-repair-commit-recovery.md` fragment 已新增且已 commit（R-09 硬性 gate，只 add 不 commit 仍 FAIL）。
- [x] `CHANGELOG.md [Unreleased]` 對應 entry（Refs #260）。
- [x] cortex CLI help 同步（R-16；task 6 的 usage 文字即為其落點，交付前重驗）。
- [x] 帶 PR 上下文執行 policy_check 確認 0 fail：`python3 -m policy_check --repo . --pr-title "..." --pr-body "..." --pr-labels "..." --pr-base-ref main --pr-head-ref "feature/260-fix-repair-commit-recovery"`。
- [x] `python3 -m pytest tests/ -q` 全綠。
