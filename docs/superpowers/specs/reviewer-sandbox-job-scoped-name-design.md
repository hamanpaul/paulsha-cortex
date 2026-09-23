---
status: accepted
work_item: reviewer-sandbox-job-scoped-name
---

# Reviewer sandbox 目錄名綁 job 與前代 claim era 孤兒 sandbox 派工時回收設計

## Decisions

### D1 job_id 進 preimage，命名只有一個導出點

`_reviewer_sandbox_name(*, run_id, card, candidate, job_id)` 是 reviewer sandbox 名稱唯一的導出點。`_create_reviewer_sandbox` 以 `job_id=<reserved_job_id>` 取 v2 名；`_reviewer_sandbox_path` 以 `job_id=job["job_id"]` 取 v2 名，另以 `job_id=None` 取 legacy 名。兩邊不再各自拼 preimage，避免 #645 那種兩個來源漂移的形狀。

選 `job_id`、不選 attempt 序號，理由如下：

- `reserved_job_id` 已在 provision 前配發（`manager.py:10944`，#648），而且配發即消耗，registry 生命週期內唯一。
- `run.attempts` 是 phase 級計數，跨 era、跨卡都不保證唯一。

保留 `sha256(...)[:32]` 形狀：容器 ACL（`0701`）、launcher 對 worktree 的處置、legacy 比對都不必跟著改，路徑也不外露可讀 job 身分。preimage 不會與 legacy 混淆：v2 preimage 比 legacy 多一段 `:job_id`，而 `run_id`／`card`／candidate（hex）都不含 `:`。

`_create_reviewer_sandbox` 的 `job_id` 是必填 keyword-only 參數、沒有預設值。預設 `None` 等於在 production 留一條會重新撞名的路，因此不給。

### D2 legacy 容忍，不搬遷既有目錄

升級當下可能有 reviewer job 正在 legacy 名目錄裡跑（cwd 就是該目錄），也可能已退出、等收割。搬遷會與執行中的 reviewer 競態，所以改採 #650 已有的「舊形狀保留為容忍面」：`_reviewer_sandbox_path` 接受 {v2 名, legacy 名}。

容忍不放寬語意：兩個名字都由該 job 自己持久化的 `workflow_run_id`／`workflow_card`／`subject_head`／`job_id` 導出；absolute、非 symlink、`parent == allowed` 三道檢查照舊。以另一顆 job 的 `job_id` 算出的 v2 名仍會被拒，job 不能冒認別人的 sandbox。新 code 只寫 v2 名，legacy 名只可能屬於升級前派出的 job。

`tests/test_reviewer_card_retry_569.py` 的兩個 forced 測試以 legacy 名造 stale sandbox，D2 讓它們原樣通過，同時充當 legacy 回歸釘。

D1 的代價是「替補 job 永遠不再沿用前一顆的 sandbox 路徑」。現有測試裡只有一處釘住舊行為：`test_operator_resume_replaces_exact_bound_reviewer_without_terminal_json` 斷言替補 job 的 `worktree` 等於舊 sandbox（`tests/test_workflow_production_wiring.py:5578-5582`）。這條斷言依 spec R6「既有斷言例外」改寫成 v2 名，不另加容忍去遷就它；scratch 原型跑完 R6 所列十個檔，只有它一處失敗。

### D3 回收掛在派工 chokepoint，不掛在 authority restart 呼叫端

會改變 claim era 的路徑有 `work_actions.py:1934`（automatic scan／resume）、`:3507`（recover-superseded），以後可能還有更多；它們全部收斂到 `_dispatch_workflow_card`。`matching` 本來就保留全 era 歷史，#765 的註解明說這是為了 retry-context 與 sandbox 清理（`manager.py:10567-10568`）。

registry 的 reset 只持 registry 狀態、拿不到 `coordinator_root`；若在 work_actions 回收，要把 coordinator root 接進 claim path，改動會跨兩個模組，而且每條新路徑仍得記得呼叫——正是本票要消除的形狀。chokepoint 回收讓「重派前回收」成為派工的結構性步驟。

執行順序：

1. `return reusable[-1]`（reuse 決策，不動任何東西）
2. #569 forced 回收（原樣）
3. `_reclaim_superseded_era_reviewer_sandboxes(matching, run=run, coordinator_root=coordinator_root)`（新）
4. planner 區塊及其後流程

之後即使 dispatch 因 decomposition、preflight 等 decision 提早返回，被回收的也只是永遠不會再被收割的前代 era 孤兒，無副作用。

### D4 回收語意：只回收前代 era 已終止的 job，candidate 比對關閉，失敗不擋派工

篩選條件見 spec R3 的 (a)–(d)，由 helper 內部從 `matching` 自行計算本 era 的 `worktree` 集合。

用 `require_candidate_unchanged=False` 的理由：跨 era 之間，Manager 自己會對同一棵 candidate 樹發佈 canonical report。具體情境是 era2 的 verification job C 被 terminalize，發佈新的 `reports/verify/*.md`。此時 review 卡前代 job B 的 `workflow_sandbox_hash` baseline 必然對不上；若用 `True`，會把 Manager 自己的發佈誤判成「reviewer 動過 candidate」，造成 fail closed 假陽性。同 era 的 #569 forced 路徑沒有這個窗口，因此維持 `True`。關閉比對不會漏掉 tracked 竄改：reuse candidate 樹時，`_require_reviewer_candidate_workspace` 會在派工當下檢查 branch、HEAD 與 `status --porcelain --untracked-files=no`，任何漂移都 fail closed。新 job 的 `workflow_sandbox_hash` 在本次派工重新快照，它自己的 terminalize 比對不受影響。

失敗處理：單筆擲 `ValueError`／`OSError` 時 `logger.warning` 後繼續。v2 名下新 sandbox 不會撞殘留，擋派工只會把一個磁碟殘留升級成 needs_human，重演本票現場。

`_discard_reviewer_sandbox` 本身不改：它在 sandbox 不存在時直接返回，所以只有殘留還在的前代 job 才會付出一次 candidate 樹 snapshot 成本。

### D5 不動的回收點

以下全部原樣：#569 forced（`True`）、terminalize（`manager.py:7296`、`:7515`）、launch failure 補償（`:11292`）、`resume_workflow_run` 內的 operator recovery（`:11580`）、job-failed（`:11847`）與 terminalize 例外（`:12122`）。`manager.py:10615-10621` 的 #569 註解只把命名描述更新為「v2 名綁 job_id；legacy 名僅容忍」，程式行為不改。

### D6 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| 同卡同 candidate 連派撞名 | 真 git repo＋`_create_reviewer_sandbox` 兩次、不同 `job_id` | 兩個目錄都存在且名稱 = `_reviewer_sandbox_name(job_id=…)`；不 raise |
| 冒認別人的 sandbox | job 的 `worktree` = 他人 `job_id` 的 v2 名 | `reviewer sandbox path invalid` |
| 升級當下的 legacy job | `worktree` = legacy 名 | `_reviewer_sandbox_path` 接受、`_discard_reviewer_sandbox` 移除 |
| #579 authority restart 後重派 | `_stuck_reviewer_run(with_git=True)`＋legacy sandbox＋`_manager_reset_workflow_for_authority_restart`＋非 forced dispatch | 新 job（v2 名）；舊目錄已刪；stuck job `exited`、`workflow_evidence is None` |
| 回收失敗 | 同上但移除 stuck 的 `workflow_repo_root` | 仍派出新 job；舊目錄在；WARNING 含 stuck `job_id` |
| 誤刪在飛／本 era／共用路徑 | 直接呼叫 helper，四種排除 job＋一顆前代 `failed` | 四者目錄保留、`failed` 被回收 |
| #569 forced drift 語意 | 既有 `test_forced_retry_fails_closed_when_the_reviewer_modified_the_candidate` | 仍 `modified Candidate checkout` |
| 既有直接呼叫點 | 三個測試補傳 `job_id`（reserve 後同 id 建 job） | 斷言不改、全綠 |
| operator recovery 換 job 不再沿用舊路徑 | 既有 `test_operator_resume_replaces_exact_bound_reviewer_without_terminal_json`（`tests/test_workflow_production_wiring.py:5578-5582` 原本斷言替補 job 沿用舊 sandbox 路徑） | 依 spec R6「既有斷言例外」改寫：替補 `worktree` = launch 路徑 = `_reviewer_sandbox_name(job_id=<替補 id>)`、同一個容器、目錄存在；舊 sandbox 已被 `:11580` 回收。這是本票唯一改寫的既有斷言 |

### D7 Sizing

只動 1 個 production 模組（`manager.py`），`domain_breadth=0`。`state_consistency=1`：命名與回收都由已持久化的 Job 欄位導出（`job_id`、`worktree`、`workflow_claim_key`、`status`），刪的是 Manager-owned 目錄，全程在單一 Manager process 內；不新增 persisted 欄位，也沒有跨物件 CAS。三件齊全時機械三維固定為 4，總分 5，落 Yellow。
