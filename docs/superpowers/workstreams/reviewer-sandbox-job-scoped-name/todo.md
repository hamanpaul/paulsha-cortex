---
status: accepted
work_item: reviewer-sandbox-job-scoped-name
domain_breadth: 0
state_consistency: 1
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Reviewer sandbox 目錄名綁 job 與前代 claim era 孤兒 sandbox 派工時回收（#579）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#579`；[spec](../../specs/reviewer-sandbox-job-scoped-name-spec.md)、[design](../../specs/reviewer-sandbox-job-scoped-name-design.md)。
- 觸及模組只有 1 個 production 模組，因此 `domain_breadth: 0`：`paulsha_cortex/coordinator/manager.py` 的 `_create_reviewer_sandbox`、`_reviewer_sandbox_path`、新增的 `_reviewer_sandbox_name`、新增的 `_reclaim_superseded_era_reviewer_sandboxes`，以及 `_dispatch_workflow_card` 的傳參與呼叫點。`state_consistency: 1`：命名與回收都由已持久化的 Job 欄位導出，在單一 process 內刪 Manager-owned 目錄，不新增 persisted 欄位。
- 不在範圍（明確排除）：
  - 不改 `registry.py`（`_manager_reset_workflow_for_authority_restart`），也不改 `work_actions.py` 的 authority restart 呼叫端（:1934、:3507）。
  - 不處理 #847（self-publication 不應觸發 authority restart）。
  - 不處理 authority restart 丟棄已 exit 0 未收割 job 成果這件事。
  - 不處理 #571（evidence 路徑不含 candidate）。
  - 不做 sandbox 全域 GC：其他 candidate、abandoned／done run、planner sandbox，以及 legacy 目錄一次性搬遷，都不在本票。
  - 不改 #569 forced 路徑 `require_candidate_unchanged=True` 的語意；planner sandbox 命名不改。
  - 不新增 CLI、persisted 欄位或 schema；不移除 legacy 容忍。
  - 不編輯 `docs/handoffs/**`。
- builder 固定規則：
  - spec／design／本 todo 的文字是 pinned authority，只准把 `[ ]` 翻成 `[x]`，其餘一個字都不改；澄清寫進 terminal reason。
  - 留在 Manager 已 checkout 的分支上工作，不得自行建立 `wt/...` 分支。
  - 不得 commit 或刪除 `docs/superpowers/plans/reviewer-sandbox-job-scoped-name.md`。
  - 測試與文件不得寫死 `openspec/changes/<change>/` 路徑（歸檔後即斷，見 #953）。

## 現場證據

- 2026-09-22 01:18 CST pin `442fe23f`：#862 run `workflow-9dc654fef3850cc68deb` 的 verification job 770 已 exit 0，收割前被 authority restart。sandbox `review-sandboxes/d9b73874009e920f49b08247fbe46d48` 沒被回收，重派同候選 `4e043feb` 時擲 `ValueError: stale reviewer sandbox requires reconciliation`，run 落 `resume-workflow-failed` needs_human、`next_actions` 只給 `abandon`；operator 以 `mv` 讓路後才 resume。
- 現行 main（7fa4716b）：
  - `manager.py:7101` 以 `sha256(run:card:candidate)[:32]` 命名、`:7102-7104` 撞名即 raise；`:7215` 以同一 preimage 驗名。
  - `:10573-10577`：`reusable` 只認本 era；回收只掛 `force_new_card`（`:10622-10627`）。
  - `:10944`：`reserved_job_id` 在 provision 前配發。
  - `registry.py:2769-2845` 的 authority restart 只拒絕 `dispatched`／`running`，不碰 sandbox。
- 既有測試釘住舊命名只有一處：`tests/test_workflow_production_wiring.py:5578-5582`（operator resume 替補 job 沿用舊 sandbox 路徑）。scratch 原型照 spec R1–R4 實作後跑 R6 十個檔，只有這個函式失敗；照 R6「既有斷言例外」改寫後轉綠。
- 離線重現：`tests/test_reviewer_card_retry_569.py` 的 `_stuck_reviewer_run(with_git=True)`＋legacy 名 sandbox＋`_manager_reset_workflow_for_authority_restart(run_id, authority_digest="e"*64)`＋非 forced `manager.dispatch_workflow_card` → 上述 ValueError；先刪該目錄再派即成功。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_reviewer_sandbox_job_scoped_579.py`，可 `from test_reviewer_card_retry_569 import _stuck_reviewer_run, _reviewer_identities, _ReviewLauncher`（沿 `tests/test_recover_planning_brainstorm_authority_728.py` 的跨檔 import 先例）。斷言逐條對應 spec R6 (a)–(f)。現行 main 必須 RED：(a) 對 `job_id` 報 `TypeError`；(b) v2 名被拒；(d) 擲 `stale reviewer sandbox requires reconciliation`；(e) 同 (d)；(f) helper 不存在。(c) 是 legacy 回歸釘，現行即綠。
- [ ] **T2 source／命名單一導出點與 sandbox 建立（R1、D1）**：新增 `_reviewer_sandbox_name(*, run_id, card, candidate, job_id)`：`job_id` 為 str → v2 preimage `f"{run_id}:{card}:{candidate}:{job_id}"`，`None` → legacy preimage `f"{run_id}:{card}:{candidate}"`，都回 `sha256(...).hexdigest()[:32]`。`_create_reviewer_sandbox` 加必填 keyword-only `job_id: str`，非空 str 以外 → `ValueError("workflow reviewer job id invalid")`，目錄名改用 v2 名。`_dispatch_workflow_card` 呼叫時傳 `job_id=reserved_job_id`。撞名守衛與訊息保留；容器、clone、checkout、remote isolation、symlink、input seed、ACL 與失敗清理逐字不變。
- [ ] **T3 source／路徑驗證認 v2 名、容忍 legacy 名（R2、D2）**：`_reviewer_sandbox_path` 讀 `job["job_id"]`，不是非空 str → `reviewer sandbox identity missing`。`path.name` 屬於 {v2 名, legacy 名} 才接受，否則 `reviewer sandbox path invalid`。absolute、非 symlink、`parent == allowed` 與 `SAFE_SHA_RE` 檢查逐字不變。`_reviewer_checkout_path`、`_discard_reviewer_sandbox`、`_is_exact_reviewer_terminal_recovery` 不另改。
- [ ] **T4 source／派工 chokepoint 回收前代 era 孤兒 sandbox（R3、R4、R5、D3、D4、D5）**：新增 `_reclaim_superseded_era_reviewer_sandboxes(matching, *, run, coordinator_root) -> None`。篩選條件依 spec R3 (a)–(d)：reviewer、前代 claim key（str 且 `!= run.claim_key`）、`status in TERMINAL_STATUSES`、`worktree` 不等於任何本 era（`workflow_claim_key in (None, run.claim_key)`）job 的 `worktree`；符合者呼叫 `_discard_reviewer_sandbox(job, coordinator_root=…, require_candidate_unchanged=False)`。單筆 `ValueError`／`OSError` → `logger.warning`（含 `run.run_id`、`job_id`、例外摘要）後繼續。`_dispatch_workflow_card` 只在 `step.persona == "reviewer"` 時呼叫，位置在 `return reusable[-1]` 之後、#569 forced 回收之後、planner 區塊之前。不改 job record。其餘回收點與 #569 `True` 語意不動；`manager.py:10615-10621` 的 #569 註解只更新命名描述。
- [ ] **T5 tests／既有呼叫點與回歸（R5、R6）**：三個直接呼叫 `_create_reviewer_sandbox` 的點補傳 `job_id`（`tests/test_builder_tasks_tick_verify_dispatch.py:150` 給固定字串；`tests/test_workflow_production_wiring.py:5286`、`:5419` 先建 registry、`reserve_job_id(<task>)`，再把同一 id 交給 `_create_reviewer_sandbox` 與 `create_job(job_id=…)`），斷言不改。唯一改寫的既有斷言依 spec R6「既有斷言例外」：`tests/test_workflow_production_wiring.py::test_operator_resume_replaces_exact_bound_reviewer_without_terminal_json` 末段 `:5578`、`:5580`、`:5581`、`:5582` 四條「替補 job 沿用舊 sandbox 路徑」改成替補 `worktree` = launch 路徑 = `_reviewer_sandbox_name(..., job_id=resumed["job_id"])`、`parent == sandbox.parent`、`workflow_input_root == worktree`、替補目錄存在、舊 `sandbox` 不存在；同函式其餘斷言不動。回歸清單須全綠，除上述一處外不改斷言：spec R6 所列十個檔（含 `tests/test_reviewer_card_retry_569.py` 的 forced 回收與 drift fail-closed 測試），外加 `tests/test_reviewer_sandbox_job_scoped_579.py`。只允許更新與新命名矛盾的測試註解／docstring。
- [ ] **T6 documentation／changelog／CLI help**：新增 `changelog.d/reviewer-sandbox-job-scoped-name.md`（`### Fixed` 一條），並同步 `CHANGELOG.md [Unreleased]`。`docs/unified-work-lifecycle.md` 在「`retry-card` 一併涵蓋 reviewer 卡（#569）」段之後補一段「reviewer sandbox 目錄名綁 job（#579）」，寫明四點：v2 名納入 `job_id`；升級前的 legacy 名仍被容忍；派新 reviewer job 前會回收前代 claim era 已終止 job 的孤兒 sandbox，回收失敗只記 warning 不擋派工；authority restart 後重派不再撞 `stale reviewer sandbox requires reconciliation`。本票不新增 CLI，`cortex work --help` 輸出不變，並以 help smoke 驗證。
