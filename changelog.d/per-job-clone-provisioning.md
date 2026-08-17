### Changed
- **#623 / trust-root Phase 2b：job 工作區模型由 `git worktree` 改為 per-job 完整
  clone——provisioning、成果回收與回收層**（Refs #623）

  **為什麼非改不可**：M1（#584）之後 builder 以 `cortex-builder` 執行、Manager 以
  `cortex-manager`，durable state 樹是 `0700 cortex-manager`。實測顯示 `git worktree`
  在這個模型下**結構性不成立**——linked worktree 的 `.git` 是指向
  `<來源 repo>/.git/worktrees/<name>` 的指標檔（在 Manager-owned 樹裡），只 chown
  worktree 目錄 `git status` 就 `fatal: not a git repository`；把 gitdir 那一格也
  chown 給 builder 之後 `git status` 過了，但 `git add` 仍失敗，因為寫 object 需要寫
  **共用** object store。推論是一條互斥：*只要 builder 要能 commit，它就必須能寫
  object store；而能寫 object store，「builder 不可竄改 Manager state」這條邊界就在
  git 這一層漏掉。* per-job clone 有自己的 object store，整個目錄由該 job 帳號擁有，
  來源 repo 對它唯讀（operator 實測 0.5 秒／35MB per job）。

  **新模組 `coordinator/job_workspace.py`**：工作區「是什麼」的單一真相——標記、識別、
  列舉、刪除、成果回收、回收前封存。三個呼叫端（`seams`／`gc`／`worktree_reclaim`）
  共用，避免同一個判準在三處各自演化。工作區的識別判準是寫在 clone `.git/` 底下的
  標記檔，**不是**「`.git` 是目錄」——後者對任何主 checkout 都成立，一筆陳舊的
  `job.worktree` 就足以讓遞迴刪除掃掉整個來源 repo（#478 現場記錄過 `job.worktree`
  等於 run 的 `workspace_root`）。標記放在 `.git/` 而非工作樹根，是因為工作樹根的任何
  檔案都會讓工作區一出生就是 dirty，而 dirty 是 `verification` 與 `gc` 的 fail-closed
  條件。

  **provisioning（`coordinator/seams.py`）**：`git worktree add` → `git clone
  --no-hardlinks`。四條守衛與 worktree 模型**逐條等價**且錯誤訊息逐字保留（既有診斷
  與 #527／#601 的測試不因實作換代而失效）：target 已存在、base 必須是既有 commit、
  既有 branch 必須完全位於 base ancestry（#613 的 fail-closed）、既有 branch
  fast-forward 後重掛。branch 仍錨定在**來源 repo**——`gc` 與 dispatch baseline 都直接
  讀它，ancestry 守衛也需要一個跨世代穩定的比較對象。clone 完成後的工作區狀態與
  worktree 模型逐字相同：`origin` 指向**真正的上游**（來源 repo 的 `origin` URL，
  delivery 的 `git -C <工作區> push origin` 因此行為不變）、指向來源 repo 的暫時 remote
  一律移除（不在 builder 的工作區裡留回寫把手）、branch 無 upstream、來源 repo 的
  `refs/remotes/origin/*` 與本地 `user.name`／`user.email` 一併複製過去（clone 不繼承
  來源的 local config，少了 identity builder 的 `git commit` 會直接失敗）。任何一步
  失敗都會把已做的變更**全部還原**（部分 clone 目錄、branch 位置），但 `target already
  exists` 這條路徑上一個位元組都不動——回滾不得擴張成「刪掉別人的工作區再說」。

  **成果回收**：clone 有自己的 object store，builder 的 commit 在回收前**來源 repo
  看不到**。新增 `job_workspace.harvest_branch()`：Manager 以 `git -C <來源 repo>
  fetch <clone> refs/heads/<branch>:refs/heads/<branch>` 單向拉回，沿用 D2「git 讀」的
  方向——builder 永遠不 push 進 Manager 的樹。refspec 刻意不帶 `+`，非 fast-forward 由
  git 拒絕，Manager 不會靜默吸收被改寫過的歷史。掛在兩個 lane 的採信點：canonical
  lane 在 `manager.apply_workflow_action` 的 build phase、`_verify_build_candidate_
  transition` 之後（candidate 已被確認是工作區 HEAD 且單調延伸自基線，回收只負責搬運，
  不新增採信路徑；回收後來源 repo 的 branch 必須恰好等於該 candidate，對不上即
  fail-closed）；slice lane 在 `verification.run_result_verification` 讀 branch head
  之前（該函式以來源 repo 為根判讀 candidate／ancestry／required-artifact diff／persona
  scope diff，少了回收整段會以 `candidate-unreadable` 收場）。工作區不是 per-job clone
  時兩處都是 no-op。

  **回收層（`coordinator/gc.py`、`coordinator/worktree_reclaim.py`）**：`gc` 的掃描同時
  涵蓋 per-job clone（走檔案系統，`git worktree list` 看不到它們）與升級前既存的 linked
  worktree；`--apply` 依工作區**自己的形狀**分派回收方式（clone＝目錄刪除、linked
  worktree＝`git worktree remove`），不依部署模式旗標——旗標會與磁碟上的實況漂移，形狀
  不會。少了 clone 掃描這一步，正在跑的 job 的 branch 會因為「沒掛在任何 worktree 上」
  被誤判為可回收。`worktree_reclaim` 的安全閘擴充為認得兩種形狀，並在刪除 clone 前把
  工作區 HEAD 封存進來源 repo 的 `refs/cortex/reclaimed/**`（來源 repo 已有該 commit
  時不重複封存）——worktree 模型下回收工作區不銷毀任何 commit，clone 模型下 `rmtree`
  會連 object store 一起刪掉，而該模組契約明文「不銷毀證據」。來源 repo 取自呼叫端或
  標記檔（既有呼叫端都不傳 `repo_root`）。

  **既有部署零回歸**：clone 模型對 `direct` 與降權模式走**同一條** code path，不依
  `PSC_JOB_RUNNER` 分支；所有新行為（回收、clone 回收路徑）都以標記檔為前置條件，
  worktree 模型與測試裡的假路徑完全不觸發。`launcher._linked_worktree_git_write_dirs`
  無需改動——它對 `.git` 為目錄的工作區本來就回空集合，而 clone 的 `.git` 在工作區內、
  已被 sandbox 的工作區授權涵蓋。

  新增 `tests/test_per_job_clone_provisioning_623.py`（27 測試，全部以真 git repo 驗證：
  provision 守衛等價與失敗回滾、工作區無回寫路徑、未回收前來源 repo 不受影響、成果
  回收與非 fast-forward fail-closed、兩個 lane 的回收掛點、clone 與 linked worktree 的
  回收、dirty 內容封存、gc 對在用 branch 的保護、端到端）。全套
  `python3 -m pytest tests/ -q`：3644 passed，零回歸。
