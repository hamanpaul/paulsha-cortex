# Issue #650：verify／review 卡的 candidate 樹仍讀前一張 build 卡的工作區——擋住「卡被採信後即時回收」

## 問題

`manager._dispatch_workflow_card()` 的 reviewer 分支以 `builder_jobs[-1]["worktree"]`
（前一張 build 卡的工作區）為 candidate 樹，六個用途全掛在它身上。#648 把 build phase
的工作區改成 per-job 之後，一個 run 會累積 N 棵這種樹（每棵約 35MB），而
`_harvest_build_candidate()` 落地之後**被採信的卡的工作區已經沒有任何獨佔資訊**
——bundle 已封存、commit 已在來源樹裡。唯一還讓它回收不掉的就是這條引用。

reviewer 卡不是降權派工的對象（`launcher._downgraded_mode()` 對 `review_only` 回
`None`），所以 #648 的 `ReadWritePaths=<pool>/%i` 不變式不落在它們身上——**這不是
blocking，是耦合**。

## 六個用途各自真正需要什麼（票上要求先查證）

| # | 用途 | 真正需要的是 | 來源樹（不 checkout）夠不夠 |
|---|---|---|:--:|
| 1 | `_authority_map_with_checkbox_tolerance(candidate_root=…)` | **candidate 內容的檔案系統視圖**——#310 的 checkbox 容忍要比對 builder 勾過的 `tasks.md` 實檔 | ✗（來源樹的工作目錄停在 `main`，是未勾的版本） |
| 2 | `_workflow_input_snapshot(repo_root=…)` | **可寫**的 candidate 內容視圖：它會 glob 宣告輸入，並把缺席的 planning authority 檔 `mkdir`＋`mkstemp`＋`os.replace` **seed 進去** | ✗（同上，且不得往共用來源樹寫） |
| 3 | `_workflow_output_baseline(…)` | **canonical report 將被發佈的那棵樹**——baseline 與 `_canonical_workflow_artifacts()` 的校驗必須同根 | ✗ |
| 4 | `_create_reviewer_sandbox(candidate_root=…)` | 只要 **object store 裡有 candidate 這個 commit**（`git clone` ＋ `checkout --detach`） | ✓ |
| 5 | `planning_runtime._tree_snapshot(…)` → `workflow_sandbox_hash` | 一棵 **reviewer 不該碰的樹**（逃逸偵測的觀測對象），且必須活過 sandbox 拆除 | ✗ |
| 6 | job 記錄的 `workflow_repo_root` | 一棵 **HEAD 恰為 candidate、可寫、跨卡穩定**的樹：`_verify_exact_candidate()` 驗 `rev-parse HEAD == candidate`；`_WorkflowReportPublicationTransaction` 往它發佈 report；**下一張 review 卡的宣告輸入就是上一張發佈進去的那份 report**（`adversarial-review.requires == code-review.produces`） | ✗ |

只有第 4 個用途「來源樹就夠」。其餘五個都要一棵**真的 checkout 在 candidate 上、且
Manager 可寫**的樹。

## 選的方向：A′（per-(run, candidate) 的 Manager-owned clone），不是票上的 A／B／C

- **B 不成立**：把「其餘四個判讀改讀 `reviewer_checkout`」套到用途 3／6 會壞兩件事——
  (a) `_discard_reviewer_sandbox()` 在 report 發佈**之前**就把 sandbox `rmtree` 掉，
  發佈目標不能在 sandbox 裡；(b) sandbox 是 per-card 的，`code-review` 發佈的 report
  無法傳給 `adversarial-review`（`workflow declared input missing`）。
- **A（per-job clone）也不成立**：同一個 (b)。per-job ⇒ 每張 review 卡一棵乾淨的樹
  ⇒ 上一張卡的 report 不在裡面。
- **C 最差**（票上已判）：把耦合固化成回收邏輯裡的特例。
- **A′**：識別穩定於 **(run, candidate)**，形狀完全沿用 #653 的
  `work_bridge._manager_ship_workspace()`。六個用途語意逐條不變，只是換成一棵 Manager
  自己的樹；candidate 前進就換一棵（新 candidate 的 review phase 本來就該從頭跑）。

## 順序問題怎麼解

票上點名「input snapshot 是 sandbox 的輸入，算它時 sandbox 還不存在」。A′ 下這個問題
**不存在**：借 #653 對 `archive-applied-needs-commit` 的處置——「同一次派工內結構性共用
同一個 provisioning」。candidate 樹在 reviewer 分支之前建好一次，authority map／input
snapshot／output baseline／sandbox clone 源／tree snapshot 全部拿到同一棵樹。

## 改法

- 新增 `manager._reviewer_candidate_workspace_id()`（唯一推導點，
  `wf-<run 摘要>-review-<candidate 前綴>`）、`_require_reviewer_candidate_workspace()`
  （branch／HEAD＝candidate／**追蹤檔**無漂移三條；未追蹤檔刻意放行——canonical report
  就是未追蹤檔，也正是卡與卡的交接載體，ship 段那條「完全乾淨」在這裡會讓第二張 review
  卡永遠開不了工）、`_reviewer_candidate_workspace()`（在來源樹上 clone，重用不打回
  pristine）。
- `_dispatch_workflow_card()` 的 reviewer 分支改用它；`builder_jobs[-1]["worktree"]`
  在該分支**完全消失**。
- `_is_exact_reviewer_terminal_recovery()` 的 candidate 樹定錨從「等於 builder job 的
  `worktree`」換成「等於唯一推導點算出來的那一棵」，舊形狀保留為升級當下的容忍面。

## 順帶收掉的一個 #641 同型缺口

舊模型下 Manager 在 reviewer 派工當下對 builder 的 clone 做的**不只是讀**（見上表用途
2／5）。三分部署下那棵樹是 `0700 cortex-builder`、且 #641 已收掉 Manager 的唯讀 ACL
⇒ 兩步都是 `Permission denied`。突變驗證逐字重現了這句話。

## 紅線遵守

- **沒有**把 Manager 對 job 工作樹的 ACL 加回來（#641／#644）——candidate 樹是 Manager
  自己 clone 的，`getfacl` 下不該有任何具名條目，稽核 5b 的「零 `setfacl`」仍成立。
- **沒有** `--reference`／`--shared`／任何把 object store 接回共用的優化（#623）。
- **沒有** `git -C <來源樹> fetch <某棵 job 的 clone>`；回收通道一個位元組沒改。

## 即時回收：拆後續票，不在本 PR

解耦之後回收**不是一行的事**：`worktree_reclaim.reclaim_worktree()` 的契約含「不銷毀
證據」（先把未提交／未追蹤內容複製到 preserve 封存再刪），而三分下 Manager **讀不進**
builder 的 `0700` clone ⇒ 那一步在降權部署下必然失敗。「誰以什麼身分回收、abandon／
retry 的重入怎麼算」自成一票 ⇒ **#658**。#650 的驗收第 3 條因此移到那裡。

## 測試

新增 `tests/test_reviewer_candidate_tree_650.py`（10 條 ＋ 1 條 skip，全部跑正式路徑：真 git repo、
真 `ScriptWorktreeCreator` 的 per-job clone、真 `dispatch_workflow_card()`、真 #637
bundle ＋ spool 交接、真 `retry-build` 重置、真 `_WorkflowReportPublicationTransaction`）。
突變驗證（把 reviewer 分支改回 `builder_jobs[-1]["worktree"]`）7 條轉紅，其中
`chmod 000` 那條逐字重現生產訊息 `PermissionError: [Errno 13] Permission denied`。
