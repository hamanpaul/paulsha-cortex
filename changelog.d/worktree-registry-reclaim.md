# worktree-registry-reclaim

- **`#478`：recover-pre-candidate 只刪目錄未清 git worktree registry，下一 tick 重派必失敗**——
  兩份各自手寫的回收片段（`manager.apply_slice_action` 的 `recover-pre-candidate` 分支、
  `work_actions._recover_pre_candidate_action`）都有同一組缺陷：前者
  `runner = git_runner or getattr(dispatcher, "_git_runner", None)`，而生產 dispatcher 的
  `_git_runner` **合法為 `None`**（它自己在 dispatch 時才 fallback），於是整段 git 清理被
  跳過、只 `rmtree` 目錄；即使 runner 存在，呼叫也多塞一個前導 `git`（seam 只吃 git 子命令
  參數，實際變成 `git -C <repo> git worktree remove ...`）。後者用裸
  `subprocess.run(["git", "worktree", ...])`（無 `-C <repo>`，跑在 manager 進程的 cwd 上）
  加 `check=False` 吞錯。兩者又都只在「目錄還在」時才嘗試清理，於是
  **「目錄已消失、registry 殘留」永遠自癒不了**——`git worktree list --porcelain` 留著
  `prunable gitdir file points to non-existent location`，下一輪
  `git worktree add` 以 `cannot force update the branch ... used by worktree at ...` 失敗，
  slice 被打回 `needs_human`（生產連續四次重現）。
  修法：新增 `coordinator/worktree_reclaim.py` 收斂成單一回收函式，契約為
  **原子性**（目錄不存在 ＋ registry 無該筆，兩個後置條件都驗證過才回報成功，任一條無法
  證實即 `failed`、呼叫端 fail closed 不回 `ok`）、**自癒**（registry 探測先於目錄探測，
  既存壞狀態照樣收乾淨，`worktree remove --force` 失敗再以 `worktree prune` 兜底）、
  **不銷毀證據**（目錄帶未提交／未追蹤內容時先複製到 `<state 檔目錄>/evidence/worktree-reclaim/`
  再刪，封存失敗即 fail closed 一個位元組都不刪——對應 issue 回報的未追蹤
  `.project-policy.yml` 被靜默刪除）。另設**安全閘**：registry 沒這筆、目錄也沒有
  linked-worktree 標記（`.git` 為檔案）時一律拒絕回收——實測 `job.worktree` 會等於 run 的
  `workspace_root`（主 checkout，`.git` 是目錄），無條件 `rmtree` 的爆炸半徑不可接受。
- **`#478`／`#527`：supersede 不回收 build worktree**——`abandon`（run 終態化為
  `superseded`）過去只清 planning artifacts，build worktree 連同它的 registry 記錄留著，
  下一世代重派同名分支必失敗。改為與 `_gc_abandoned_planning_artifacts` 同一掛載點呼叫
  同一支回收函式，範圍以 job 的 `workflow_run_id` 精準框定（與 `emit_outcome` 同一條歸屬
  判準）；紀律亦相同——回收是 abandon 的附帶效果，失敗只落 diagnostics，不得讓已成立的
  abandon 反悔。
- **`#535`：abandon 後的 planning evidence 佔住 content-addressed 命名空間**——brainstorm
  evidence 檔名原本只由 `(scope, question_pack_id)` 決定，前一世代 abandon 後檔案仍在，
  下一世代重跑 brainstorm 落點完全相同；模型輸出語意相同但 byte 不同，撞上 `publish()`
  的 no-clobber fail-closed，新世代必然以
  `ValueError: planning artifact no-clobber conflict` 收場。
  修法採 issue 建議 **(b) 命名空間帶 run identity**，而非 (a) 的「abandon 時搬走前代
  evidence」：**取捨理由**是 evidence 不可銷毀（審計不可變原則）意味著也不該被**搬動**——
  前代 run 的 `gate_refs`／`evidence_refs` 逐字記著絕對路徑，搬檔會讓那些稽核指標整批
  懸空，等於用「稽核紀錄失聯」換「命名空間騰出」。帶 run identity 則前代 evidence 原位
  不動、原路徑仍可稽核，新世代自然不撞，且無須在 abandon 路徑新增檔案搬移這種不可逆
  副作用。檔名改為 `brainstorm-<run_id>-<hash>.json`，run_id 同時進 hash 輸入（手改檔名
  偽造不出同一份 content address）；未帶 run identity 時退回舊命名，既有殘留檔仍可讀。
  **世代內**的衝突偵測不放寬——同一個 run 兩次輸出不同仍舊 fail closed。
- **`#535` 建議 3：no-clobber 衝突訊息附上歸屬**——訊息補
  `existing owner=<run_id|legacy-unscoped> mtime=<ISO> publishing run=<run_id>`，operator
  不必再自己挖 mtime 對時間軸判斷殘留檔屬於哪個世代。
- **測試**：`tests/test_worktree_registry_reclaim_478.py` 以**真實** temporary git repo ＋
  真實 linked worktree 驗證 registry 後置條件（含「同一 feature branch 立刻可重掛」與
  `ScriptWorktreeCreator.create()` 重建成功，即下一 tick 的實際動作）；
  `tests/test_pre_candidate_recovery.py` 的既有 fixture 一併從普通暫存目錄升級為真實
  linked worktree——issue 明確指出正是那個 fixture 讓缺陷四次在生產重現而測試全綠。
  `tests/test_planning_evidence_generation_scope_535.py` 釘住跨世代不衝突、世代內仍
  fail closed，並以「不帶 run identity」版本重現根因。
