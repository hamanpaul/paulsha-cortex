### Added

- **#623 / trust-root Phase 2b：per-job clone 的信任根層——`repo-source-tree`、**三份**
  root-owned `.gitconfig` 與 `commit-spool` 進登記表，內容由 permgen 產生**——Phase 2b
  M1 之後實機發現「這個部署做不了真實工作」：`ProtectHome=yes` 讓 `/home` 完全不可見，
  而登記表**沒有定義 repo 源碼樹該放哪**。實測進一步證明 `git worktree` 在三分下結構性
  不成立——worktree 的 `.git` 指向**共用 object store**，builder 只要 `git add` 就必須能寫
  該 store，「builder 能 commit」與「三分隔離」互斥。裁決改為 **per-job 完整 clone**
  （0.5 秒／35MB per job），本 PR 落地其信任根層：

  - 登記表新增 **`repo-source-tree`**（`<agents_root>/repos`，Tier-0、MANAGER_OWNED）：
    **working checkout**（不是 bare——monitor 掃的是工作樹裡的 `workstreams/*/todo.md`
    等檔案），同一份 checkout 兼作 monitor 掃描目標與 job 的 clone 來源。**writer 是
    Manager**（0817 裁決，推翻本票初版的 root-owned）：`git fetch` 必須把 `FETCH_HEAD`
    寫進**目標 repo**，而 #634 的成果回收正是「fetch 進來源樹」，provisioning 那半邊的
    `git branch -f` 同樣是對來源樹的寫入——實機在 root-owned 下實測
    `error: cannot open '.git/FETCH_HEAD': Permission denied`，**「Manager 唯讀」與
    「Manager 回收成果」互斥**，取後者。機械落點是 `owner_class=MANAGER_STATE`
    （owner＝`cortex-manager` 0700），兩個 job 帳號各獲一條**唯讀** ACL（`rX`），
    monitor 則靠 unit 的 persona 過濾（#622）仍寫不進去。隔離未變弱：不受信任的是 job
    帳號，而 Manager 本來就擁有 gate ledger／evidence／`jobs.json`。
  - 登記表新增 **`builder-gitconfig`／`reviewer-planner-gitconfig`／`manager-gitconfig`**
    （Tier-0、root-owned 0644、落在各自帳號 HOME 下）：比照既有的 `codex-hooks`。跨擁有者
    的 git 操作會被 dubious-ownership 保護擋下（`fatal: detected dubious ownership`），
    唯一解是 `safe.directory`，而它**必須由 root 放進該帳號的 HOME**——那些 HOME 都是
    root-owned，帳號自己放不了這個檔。**Manager 那份是實機複驗補上的 blocking 缺口**：
    來源樹是 root 建立後才 chown 過去的，owner 不相符的中途狀態會讓 Manager 的每一個 git
    操作失敗。**內容**也由 permgen 產生（`build_account_gitconfig()` ＋ CLI
    `trust_root gitconfig [--builder|--reviewer-planner|--manager] --source-repo <slug>`），
    比照 shim／polkit，不手寫；每個來源 repo 產生**兩條** `safe.directory`（工作樹根 ＋
    `<root>/.git`）——實測從**非 bare** 來源 clone 時 git 檢查的是後者，`git -C <repo> …`
    報的是前者，只給一條會讓另一半的操作在完全不同的時機才失敗。來源 repo 清單是**部署
    決定**（比照 #626），未宣告即 fail-closed：git 的 `safe.directory` 只認**逐字相等**的
    路徑或字面 `*`（實測 git 2.43：`<repos>/*` 仍被拒），而字面 `*` 等於對該帳號整個關掉
    這個保護。
  - 登記表新增 **`commit-spool`**（`<coordinator_root>/commit-spool/<job-id>/`，Tier-0、
    JOB_VISIBLE、`INTERPROCESS`）＋ path resolver `config.paths:commit_spool_root()`：
    成果回收改走 **bundle ＋ append-only spool**（0817 裁決）。#634 現行的「Manager 伸手
    進 builder 的 clone `fetch`」需要 (a) traverse 進 builder-owned 的 `0700` 樹——實測
    `Permission denied`；(b) 為**每個 job 路徑**加 `safe.directory`——而 git 2.43 不吃路徑
    glob，等於把 Manager 的 Tier-0 gitconfig 變成執行期可變狀態。改走 bundle 後 builder 在
    自己的 clone `git bundle create` 寫進本 spool，Manager 從那個 **bundle 檔**（不是 repo）
    fetch：Manager 全程不碰 builder 的樹，dubious-ownership 與 traverse 兩個問題同時消失。
    形態**逐條比照 `review-verdict-spool`**：容器 owner＝`cortex-manager` 0700，producer 僅
    獲 **`wx` 無 `r`** 的 per-account ACL，per-job 目錄由 Manager 在 dispatch 當下建立、
    落地後轉唯讀。**producer 只有 builder**——登記表裡唯一以 git commit 交付的 persona 就是
    它（`repo-worktree` 的 writer 只有 BUILDER），reviewer 的交付通道是
    `review-verdict-spool`、planner 的是 `dispatch-specs-tree`；多授一個 `wx` 給沒有 producer
    的帳號只是多開一條無人消費的寫入面。本 PR **只定義資產與權限**，bundle 的產生／消費在
    coordinator 側，屬後續變更。
  - **unit 產生器**：Manager unit 明寫來源樹**可寫**並附裁決理由，monitor unit 明寫它在
    monitor 這側仍唯讀（並點出這是 persona 過濾而非帳號差異）；job 模板 unit 明寫 clone
    來源唯讀、clone 落點 `<worktree>/%i` 與 commit spool 在 RWP 內。monitor 對 Manager 的
    **真子集**不變式（#622）仍成立——來源樹只進 Manager 那一側。
  - `PathLayout` 新增 `repo_source_root`／`commit_spool_root`／`gitconfig_of()`／
    `manager_account`／`source_repo_slugs`／`source_repo_safe_directories()`
    （＋`with_source_repo_slugs()` 的 slug 形狀驗證），`with_job_segment()` 改用
    `dataclasses.replace`——逐欄位重建會讓每個新欄位靜默掉回預設值。

  新增 `tests/test_trust_root_repo_source_tree_623.py`（68 測試：兩個 job 帳號對來源樹零
  寫入、Manager 可寫而 monitor 不可、commit spool 的 `wx` 無 `r` 與 producer 面、traverse
  鏈完整（沿用 #624 的 `unreachable_hops()`）、三份 `.gitconfig` 的兩條 `safe.directory`、
  fail-closed 與 CLI 兩條注入管道）；runbook 補第 2c 步、spec §R1 補兩段裁決。
