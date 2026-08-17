### Fixed
- **#641 / trust-root：`repo-worktree` 仍授 Manager 唯讀 ACL——交換面已改 bundle，
  這條授權沒有消費者，卻讓 #637 的不變式在實機上不成立**（Closes #641）

  #637 把成果回收整條換成 **bundle ＋ append-only spool**（builder 在自己的 clone
  產 bundle → 寫進 Manager-owned 的 `commit-spool` → Manager 從**那個檔案** fetch），
  並為此加了不變式測試 `test_manager_never_touches_the_builder_clone_while_harvesting`
  （把 clone `chmod 000` 仍能完成回收）。但登記表 `repo-worktree` 的 rationale 仍停在
  worktree 時代——「trusted reader（Manager）以唯讀 ACL 讀取（交換面沿用 D2 git 讀）」
  ——permgen 因此機械產出 `setfacl -m u:cortex-manager:rX /var/lib/cortex/worktree/<job-id>`。
  operator 0817 實機複驗：

  ```
  只有 0700、無 ACL          → Manager `ls` 得到 Permission denied（#637 的不變式成立）
  套上登記表要求的那條 ACL    → Manager 讀得到（同一條不變式在實機上不成立）
  ```

  也就是**測試裡成立的不變式，在照登記表部署的機器上不成立**，因為登記表主動授了
  那條已無消費者的讀取權。

  **三條一起收，不是只收一條。** job 樹底下原本有**三條**同型授權，全部出自 permgen
  「單一 job writer」分支那一句共用 rationale：`repo-worktree` 的 `rX`、
  `review-verdict`（reviewer 工作樹內的 legacy verdict 檔）的 `r`、以及
  `work-items-yaml`（`<job 樹>/.cortex/work-items.yaml`）的 `r`。後兩條逐條論證過
  沒有消費者：verdict 的權威通道自 Phase 2a 起是 `review-verdict-spool`，而
  `_review_verdict_source()` 對帶 `review_verdict_channel == "spool"` 標記的 job
  **只**認 spool、明確不回退讀 worktree——Phase 2b 部署派出的每個 reviewer job 都帶
  那個標記，legacy 分支只對 Phase 2a 之前派工的 in-flight job 成立，而那批 job 不可能
  出現在一台已經套了三分 ACL 的機器上；`work-items.yaml` 則是 monitor 讀**來源樹**
  那一份（`correlation.load_work_item_overrides()` 與 `work_actions` 都以 Manager 自己的
  `PSC_REPO_ROOT` 為根，也就是 `repo-source-tree`），builder 的修改是 git-tracked，
  循 bundle 以 commit 旅行、不以跨帳號檔案讀取旅行。**必須一起收**的機械理由：
  traverse ACL 是由跨帳號 ACL 導出的（#620），留任何一條，`<job 樹>`（乃至
  `<job 樹>/.cortex`）的 `--x` 就會自己長回來。

  **這不只是最小權限，也是一條提權路徑的成立條件。** 那條 `rX` 唯一還在使用的消費端
  是 `coordinator/verification.py`——它不只讀 HEAD／dirty 狀態，還以
  `cwd=<job 樹>` **執行**宣告出來的 check／test／full-suite（`_resolve_worktree_cwd`）。
  `pytest` 會載入該樹的 `conftest.py`／plugin，等於 builder 在 `cortex-manager` 身分下
  取得任意程式碼執行——與 #629 在 gate 執行面描述的是同一條路徑（同一段論證早已寫在
  `gate-ledger` 的 note 與 `launcher._should_run_gates` 的 docstring）。收掉這條讀取權
  之後，那些命令在三分下連 spawn 都不會成功。

  **verification 那組讀工作樹的檢查改為明確 fail-closed 並指向 #629**（方向 1，不是
  靜默略過、也不是整組刪掉）。`candidate-worktree-unreadable`／`-status-error`／
  `-moved-after-verification` 三個既有理由碼保留給「那棵樹壞了」；讀取失敗**是權限
  造成的**時改回專屬理由碼 `candidate-worktree-unreadable-pending-gate-identity`，
  evidence 的 `details.candidate_worktree_blocked` 帶 `blocked_on: "#629"` 與可操作
  處置（等 #629 的第三執行身分，或在 `PSC_JOB_RUNNER=direct` 下重跑）。canonical lane
  的 `_verify_exact_candidate`／`_verify_build_candidate_transition` 得到同一處理：
  處置一格未變（照樣 raise），只是訊息從那個部署形態下毫無意義的「candidate does not
  exist」換成指得出下一步的字串。三條 **MUST NOT** 逐字寫進 `verification.py` 的
  模組註解以免日後被「順手修好」：不得靜默略過；不得改讀 bundle 替代（bundle 同樣
  由 builder 產生，兩邊同源會讓檢查退化成「builder 說什麼就是什麼」）；不得讓 builder
  自報工作樹乾淨（違反 #540 的 acceptance chain 與 #628 的證據作者歸屬）。

  **不是回歸**：三分部署下 build 卡本來就因 `require_ledger` 而
  `gate-ledger-missing` fail-closed（#629 記錄的現況），這條 lane 早已卡在同一票；
  `direct` 模式與所有既有測試環境下 Manager 就是工作樹的 owner，一位元都沒變。

  **reviewer 側同型殘留已一併確認**：除上述 `review-verdict` 之外沒有其他殘留——
  `_verify_exact_candidate` 對 reviewer persona 取的是 `workflow_repo_root`
  （`repo-source-tree`，Manager-owned）而不是 reviewer 的工作樹，verdict 走 spool，
  reviewer 的工作樹從頭到尾沒有被 Manager 讀過。`dispatch-worktree-pool` 容器層
  （`0701`，owner＝Manager）也複驗過：產生器對它只出 `install -d`／`chown`／`chmod`，
  零 `setfacl`，沒有任何為「Manager 讀 job 樹」而設的授權。

  **測試**：新增 `tests/test_manager_worktree_acl_641.py`（31 測試）。結構性那一組
  （權限計畫零跨帳號 ACL、產生的 script 含**註解掉的 per-job 段**都沒有指向 job 樹的
  `setfacl`、rationale 不再宣稱 Manager 讀取、兩個 scheme 各驗一次）在任何環境都跑；
  **OS 層那一組**（照登記表建出 `0700 <job uid>` 的樹，另一個 uid `listdir`／`open`
  必須被拒，而 job 帳號自己讀得到）需要 root 才借得到兩個身分，非 root 時**明確 skip
  並附理由**——這正是 #638 點名的「單 UID 環境測不出來的 OS 語意」，不靜默通過。
  **突變驗證**四組全部實跑過：把 `repo-worktree` 的 reader 改回 Manager → 7 條紅；
  只把 `work-items-yaml` 改回去 → 6 條紅（含 `test_no_traverse_grant_reaches_into_a_
  job_worktree`，證明 traverse 真的會自己長回來）；拿掉 `worktree_read_blocked()` 的
  偵測 → 6 條紅；OS 層把修法前那條 `u:<manager>:rX` 以 xattr 套回去 → 讀取從被拒轉為
  成功（證明該 fixture 分辨得出兩種形狀，不是因為建錯東西才紅）。
  `tests/test_trust_root_permgen_traverse_620.py::test_unknown_intermediate_directory_
  is_fail_closed` 改以**合成資產**驗被驗的性質——它原本綁在 `work-items-yaml` 這個
  登記表資料上，那個耦合正是它會隨無關變更一起紅掉的原因。

  docs 同步：spec §R1 新增「job 工作樹對 Manager 完全不可讀（#641）」一節、修正
  §背景資產表與 Phase 2 roadmap 第 6 條的「沿用 D2 git 讀」；runbook 第 2b 步新增
  稽核 5b（`grep setfacl … /var/lib/cortex/worktree/` 期望空輸出）。

  理由碼的判定字串是**實測**來的：以 git 2.43.0 對一個 `0700`、屬於別的 uid 的
  真實 repo 跑 `rev-parse HEAD`／`status --porcelain`／`merge-base --is-ancestor`，
  三者逐字都是 `fatal: cannot change to '<dir>': Permission denied`、rc 皆為 128。

  已 rebase 到 #642（executor toolchain ＋ per-account 憑證）之上：它新增的兩個資產
  都不在 job 工作樹底下（`<deploy_root>/toolchain` 與 builder HOME 下的憑證檔），
  本票的不變式因此不受影響——而且是**機械性**地不受影響（那條斷言掃的是「落點在
  job 樹底下的每一項」，不是一份硬編碼的 asset_id 清單）。

  全套 `python3 -m pytest tests/ -q`：**3834 passed, 4 skipped**（零回歸；4 skipped
  ＝#638 既有 2 條 ＋ 本次新增的 2 條 root-only 不變式）。那 2 條已另以 root 實跑：
  `sudo python3 -m pytest tests/test_manager_worktree_acl_641.py -q` → **31 passed**，
  含跨 UID 的正向（job 帳號讀得到）、反向（Manager 被拒）與突變對照（套回舊 ACL 就
  讀得到）。
