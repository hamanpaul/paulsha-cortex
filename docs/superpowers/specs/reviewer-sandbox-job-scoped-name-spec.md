---
status: accepted
work_item: reviewer-sandbox-job-scoped-name
---

# Reviewer sandbox 目錄名綁 job 與前代 claim era 孤兒 sandbox 派工時回收規格

## Requirements

對應 [#579](https://github.com/hamanpaul/paulsha-cortex/issues/579)：reviewer sandbox 目錄名是 `sha256(run_id:card:candidate)[:32]`，同 run、同卡、同 candidate 的第二顆 job 必然算出同名；只要有一條重派路徑沒先回收舊 sandbox，`_create_reviewer_sandbox` 就擲 `stale reviewer sandbox requires reconciliation`，run 落 needs_human、`next_actions` 只剩 `abandon`。authority restart（`registry._manager_reset_workflow_for_authority_restart`）正是這樣一條路徑。本票修兩件事：命名納入 job 身分，從根上消除撞名；在派工 chokepoint 回收前代 claim era 的孤兒 sandbox，避免撞名消失後變成靜默的磁碟殘留。

1. **R1 sandbox 目錄名綁 job**：新增模組級單一導出點 `_reviewer_sandbox_name(*, run_id: str, card: str, candidate: str, job_id: str | None) -> str`。`job_id` 為 str 時回 `hashlib.sha256(f"{run_id}:{card}:{candidate}:{job_id}".encode()).hexdigest()[:32]`（下稱 v2 名）；`job_id is None` 時回 `hashlib.sha256(f"{run_id}:{card}:{candidate}".encode()).hexdigest()[:32]`（下稱 legacy 名，與 7fa4716b 逐字相同）。`_create_reviewer_sandbox` 新增必填 keyword-only 參數 `job_id: str`，非空 str 以外一律 `ValueError("workflow reviewer job id invalid")`，目錄名改用 v2 名。`_dispatch_workflow_card` 傳入 `reserved_job_id`：它在 provision 前就已配發，而且配發即消耗（`registry.reserve_job_id`，#648）。同 run／card／candidate、不同 `job_id` 的兩次呼叫必須得到兩個不同且都存在的目錄，第二次不 raise。`sandbox.exists() or sandbox.is_symlink()` 守衛與訊息 `stale reviewer sandbox requires reconciliation` 保留作縱深防禦。以下都逐字不變：容器目錄（`_reviewer_sandbox_parent`、`0701`）、32 hex 名長、clone／checkout／remote isolation／external symlink／input seed／ACL 授權流程，以及所有失敗時的 `shutil.rmtree` 清理。
2. **R2 路徑驗證認 v2 名並容忍 legacy 名**：`_reviewer_sandbox_path(job, coordinator_root)` 以 `job["job_id"]` 導出 v2 名；`job_id` 不是非空 str → `ValueError("reviewer sandbox identity missing")`。只有 `path.name` 屬於 {v2 名, legacy 名} 才接受；以別的 `job_id` 算出的 v2 名 → `ValueError("reviewer sandbox path invalid")`。其餘檢查逐字不變：absolute、非 symlink、`path.parent == allowed`，以及 `workflow_run_id`／`workflow_card`／`subject_head` 的型別與 `SAFE_SHA_RE`。legacy 容忍只為升級當下已派出或已退出、目錄仍是 legacy 名的舊 job。它們的 terminalize、operator recovery、#569 forced 回收與 R3 回收都要驗得過；新派 job 一律寫 v2 名。`_reviewer_checkout_path`、`_discard_reviewer_sandbox`、`_is_exact_reviewer_terminal_recovery` 都經由本函式，自動涵蓋，不另改。
3. **R3 前代 claim era 孤兒 sandbox 在派工 chokepoint 回收**：新增 `_reclaim_superseded_era_reviewer_sandboxes(matching: Sequence[Mapping[str, object]], *, run, coordinator_root: str | Path) -> None`。`_dispatch_workflow_card` 只對 `step.persona == "reviewer"` 呼叫它，位置在 `if reusable and not retryable_latest: return reusable[-1]` 之後（已決定要派新 job），並排在既有 #569 forced 回收區塊之後、planner 區塊之前。對 `matching` 中同時滿足以下四項的 job，呼叫 `_discard_reviewer_sandbox(job, coordinator_root=coordinator_root, require_candidate_unchanged=False)` 回收其 sandbox：(a) `persona == "reviewer"`；(b) `workflow_claim_key` 是 str 且 `!= run.claim_key`；(c) `status in TERMINAL_STATUSES`（`exited`／`failed`）；(d) `worktree` 不等於任何本 era job 的 `worktree`（本 era 指 `workflow_claim_key in (None, run.claim_key)`）。只刪 sandbox，不改 job record（`status`、`workflow_evidence`、`worktree` 原樣保留稽核）。
4. **R4 回收不擋派工、不誤刪**：R3 回收單一 job 時若擲 `ValueError` 或 `OSError`，以 `logger.warning` 記一行（含 `run.run_id`、該 job 的 `job_id` 與例外摘要）後繼續下一筆，dispatch 照常往下走。v2 名保證新 sandbox 不會撞到殘留目錄。in-flight job（`dispatched`／`running`）、本 era job、`workflow_claim_key is None` 的 job，以及 `worktree` 與本 era job 相同的前代 job，一律不碰。
5. **R5 既有回收點與相鄰命名不變**：以下呼叫點與參數逐字不改：#569 forced 路徑（`require_candidate_unchanged=True`）、`terminalize_workflow_job` 兩處（`manager.py:7296`、`:7515`）、`_dispatch_workflow_card` 的 launch failure 補償（`:11292`），以及 `resume_workflow_run` 內三處：operator recovery（`:11580`）、job-failed（`:11847`）、terminalize 例外（`:12122`）。planner sandbox 命名 `sha256(run_id:card)`（`_discard_failed_planner_sandbox`、`_planner_sandbox_path`）不改。
6. **R6 測試**：`tests/test_reviewer_sandbox_job_scoped_579.py` RED→GREEN：
   - (a) 同 run／card／candidate、不同 `job_id` 兩次 `_create_reviewer_sandbox` → 兩個不同且都存在的目錄，名稱各等於 `_reviewer_sandbox_name(..., job_id=<該 id>)`，第二次不 raise。
   - (b) `_reviewer_sandbox_path`：`worktree` 為自己 `job_id` 的 v2 名 → 回該路徑；為另一個 `job_id` 的 v2 名 → `reviewer sandbox path invalid`；job 缺 `job_id` → `reviewer sandbox identity missing`。
   - (c) legacy 容忍：`worktree` 為 legacy 名的 reviewer job → `_reviewer_sandbox_path` 接受，`_discard_reviewer_sandbox` 移除該目錄。
   - (d) #579 現場重建：沿用 `tests/test_reviewer_card_retry_569.py` 的 `_stuck_reviewer_run(with_git=True)`，stuck job 指向真實存在的 legacy 名 sandbox（比照同檔 `test_forced_retry_recycles_the_superseded_reviewer_sandbox` 的欄位改寫），再 `registry._manager_reset_workflow_for_authority_restart(run.run_id, authority_digest="e" * 64)`，之後以非 forced 的 `manager.dispatch_workflow_card` 派工。預期：回新 job（`job_id` 不同於 stuck，`worktree` 為 v2 名且目錄存在）；舊 sandbox 已不存在；stuck job 的 `status == "exited"` 且 `workflow_evidence is None`。
   - (e) 同 (d)，但移除 stuck job 的 `workflow_repo_root`（回收必擲 `ValueError`）→ dispatch 仍回新 job，舊目錄仍在，`caplog` 有一筆含 stuck `job_id` 的 WARNING。
   - (f) 直接呼叫 `_reclaim_superseded_era_reviewer_sandboxes` 驗排除面：前代 era 但 `status == "running"`、本 era `exited`、`workflow_claim_key is None`、前代 era 但 `worktree` 與本 era job 相同，四者目錄都保留；前代 era 且 `status == "failed"` → 目錄被回收。

   (a)(b)(d)(e)(f) 在現行 main 必須 RED；(c) 是 legacy 回歸釘，現行即綠。既有 `tests/test_reviewer_card_retry_569.py`、`tests/test_reviewer_candidate_tree_650.py`、`tests/test_reviewer_sandbox_handover_742.py`、`tests/test_builder_tasks_tick_verify_dispatch.py`、`tests/test_workflow_production_wiring.py`、`tests/test_immediate_worktree_reclaim_658.py`、`tests/test_canonical_per_job_workspace_648.py`、`tests/test_retry_feedback_context_606.py`、`tests/test_midchain_builder_retry_545.py`、`tests/test_candidate_base_refreeze_731.py` 原斷言保留，唯一例外見下方「既有斷言例外」。其中三個直接呼叫 `_create_reviewer_sandbox` 的點（`tests/test_builder_tasks_tick_verify_dispatch.py:150`、`tests/test_workflow_production_wiring.py:5286`、`:5419`）只補傳 `job_id`：之後會以 registry 建 job 的，先 `registry.reserve_job_id(<task>)`，再把同一個 id 交給 `_create_reviewer_sandbox(job_id=…)` 與 `create_job(job_id=…)`，這三處的斷言一條不改。
   - **既有斷言例外（唯一允許改寫的既有斷言）**：`tests/test_workflow_production_wiring.py::test_operator_resume_replaces_exact_bound_reviewer_without_terminal_json` 末段四條「替補 job 沿用舊 sandbox 路徑」斷言（7fa4716b `:5578` `launched[0][2] == str(sandbox)`、`:5580` `Path(replacement["worktree"]) == sandbox`、`:5581` `Path(replacement["workflow_input_root"]) == sandbox`、`:5582` `sandbox.is_dir()`）。R1 之後替補 job 取自己 `job_id` 的 v2 名，operator recovery（`manager.py:11580`）先回收了舊 job 的 sandbox，這四條在修好的 code 上必然失敗，屬於直接釘住舊命名的斷言。改寫為：`launched[0][2] == replacement["worktree"]`；`Path(replacement["worktree"]).parent == sandbox.parent`；`Path(replacement["worktree"]).name == _reviewer_sandbox_name(run_id=run.run_id, card=verify_step.card, candidate=candidate, job_id=resumed["job_id"])`；`Path(replacement["workflow_input_root"]) == Path(replacement["worktree"])`；替補目錄存在；舊 `sandbox` 已不存在。同函式其餘斷言（`reason`、`job_id` 不同、launch 次數、`candidate_checkout`、舊 job `workflow_evidence is None`）與 `_is_exact_reviewer_terminal_recovery` 的正反例一條不改。

## Boundary

Production 只改 `paulsha_cortex/coordinator/manager.py`：`_create_reviewer_sandbox`、`_reviewer_sandbox_path`、新增 `_reviewer_sandbox_name`、新增 `_reclaim_superseded_era_reviewer_sandboxes`，以及 `_dispatch_workflow_card` 的傳參與呼叫點。以下不在本票範圍：

- 不改 `registry.py` 的 `_manager_reset_workflow_for_authority_restart`，也不改 `work_actions.py` 的 authority restart 呼叫端（:1934 automatic／resume、:3507 recover-superseded）。回收集中在派工 chokepoint，任何 era 變更路徑都自動涵蓋。
- 不處理 #847：自身 frozen planning 同內容發布不應觸發 authority restart。
- 不處理 authority restart 丟棄「已 exit 0 但未收割」reviewer job 成果這件事（先收割再 restart 屬 restart 語意）。
- 不處理 #571：evidence 路徑不含 candidate。
- 不做 sandbox 全域 GC：其他 candidate、abandoned／done run 殘留的 reviewer sandbox、planner sandbox，以及既有 legacy 目錄的一次性搬遷，都留給後續票。
- 不改 #569 forced 路徑 `require_candidate_unchanged=True` 的語意。
- 不新增 CLI、persisted 欄位或 schema；不移除 legacy 容忍（待無 legacy job 後另票清）。
- 不編輯 `docs/handoffs/**` 歷史紀錄。

## Evidence

- #579 本文（PR #574 回報）：同卡同 candidate 重派必然撞名，每條重派路徑都得記得先回收。
- 2026-09-21T17:33Z 留言（2026-09-22 01:18 CST，pin `442fe23f`）：#862 run `workflow-9dc654fef3850cc68deb` 的 verification job 770 exit 0、result `verified`，收割前因 `source_revision` 前進被 authority restart（`retry_classification: authority_restart`）。sandbox `review-sandboxes/d9b73874009e920f49b08247fbe46d48` 沒被回收；下一 tick 重派同候選 `4e043feb` 時擲 `ValueError: stale reviewer sandbox requires reconciliation`，run 落 `resume-workflow-failed` needs_human、`next_actions` 只給 `abandon`；operator 以 `mv … .stale-verification-770` 讓路後才 resume。`docs/handoffs/2026-09-23-refine-b2-handoff.md:92` 記同一處置。
- 7fa4716b 程式碼：
  - `paulsha_cortex/coordinator/manager.py:7101` 以 `sha256(f"{run.run_id}:{step.card}:{candidate}")[:32]` 命名，`:7102-7104` 撞名即 raise。
  - `:7215` 的 `_reviewer_sandbox_path` 以同一 preimage 驗名。
  - `:10573-10577`：`reusable` 只認本 claim era；`:10613-10614` 前代 era 的 job 不會被 reuse。
  - `:10622-10627`：回收只掛 `force_new_card`。
  - `:10944`：`reserved_job_id` 在 provision 前配發；`:11107` 呼叫 `_create_reviewer_sandbox`。
  - `paulsha_cortex/coordinator/registry.py:2769-2845` 的 authority restart 只拒絕 `ACTIVE_JOB_STATUSES`（`dispatched`／`running`，`registry.py:32`），不碰任何 sandbox；呼叫端 `work_actions.py:1934`、`:3507` 也沒有回收動作。
- 既有測試釘住舊命名的唯一一處：`tests/test_workflow_production_wiring.py:5578-5582` 斷言 operator resume 的替補 job 沿用舊 sandbox 路徑。scratch 原型（R1–R4 照本 spec 實作）跑 R6 所列十個檔，只有這一個函式失敗，其餘全綠；照 R6「既有斷言例外」改寫後轉綠。
- 離線重現（scratch，7fa4716b）：`_stuck_reviewer_run(with_git=True)`＋legacy 名 sandbox＋authority restart＋非 forced `dispatch_workflow_card` → `ValueError: stale reviewer sandbox requires reconciliation`。先移除該目錄再派，同一 dispatch 成功回 `wf-…-verification-3`。
