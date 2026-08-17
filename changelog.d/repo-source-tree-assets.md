### Added

- **#623 / trust-root Phase 2b：per-job clone 的信任根層——`repo-source-tree` ＋ 兩份
  root-owned `.gitconfig` 進登記表，內容由 permgen 產生**——Phase 2b M1 之後實機發現
  「這個部署做不了真實工作」：`ProtectHome=yes` 讓 `/home` 完全不可見，而登記表
  **沒有定義 repo 源碼樹該放哪**。實測進一步證明 `git worktree` 在三分下結構性不成立
  ——worktree 的 `.git` 指向**共用 object store**，builder 只要 `git add` 就必須能寫
  該 store，「builder 能 commit」與「三分隔離」互斥。裁決改為 **per-job 完整 clone**
  （0.5 秒／35MB per job），本 PR 落地其信任根層：

  - 登記表新增 **`repo-source-tree`**（`<agents_root>/repos`，Tier-0、MANAGER_OWNED）：
    **working checkout**（不是 bare——monitor 掃的是工作樹裡的 `workstreams/*/todo.md`
    等檔案），同一份 checkout 兼作 monitor 掃描目標與 job 的 clone 來源。**writer 只有
    部署身分（root）**，因此 owner_class 機械分到 `DEPLOYMENT`、owner＝root 0755，
    Manager／monitor／兩個 job 帳號**一律唯讀**——ReadWritePaths 純由「誰可寫」導出，
    owner＝`cortex-manager` 會讓 Manager unit 自動拿到寫入權，「Manager 唯讀」與
    「owner＝Manager」互斥，取前者（Manager 被攻陷也改不了每個 job clone 的來源；
    代價是更新來源樹改為 operator 的 root 動作）。
  - 登記表新增 **`builder-gitconfig`／`reviewer-planner-gitconfig`**（Tier-0、
    root-owned 0644、落在各自 job 帳號 HOME 下）：比照既有的 `codex-hooks`。跨擁有者
    clone 會被 git 的 dubious-ownership 保護擋下（`fatal: detected dubious ownership`），
    唯一解是 `safe.directory`，而它**必須由 root 放進 job 的 HOME**——job 的 HOME 是
    root-owned，它自己放不了這個檔。**內容**也由 permgen 產生（`build_job_gitconfig()`
    ＋ CLI `trust_root gitconfig [--builder|--reviewer-planner] --source-repo <slug>`），
    比照 shim／polkit，不手寫。來源 repo 清單是**部署決定**（比照 #626），未宣告即
    fail-closed：git 的 `safe.directory` 只認**逐字相等**的路徑或字面 `*`（實測
    git 2.43：`<repos>/*` 仍被拒），而字面 `*` 等於對該帳號整個關掉這個保護。
  - **unit 產生器**：Manager／monitor unit 明寫來源樹在此唯讀（`ProtectSystem=strict`
    下讀是預設允許，而 RWP 機械地不涵蓋它）；job 模板 unit 明寫 clone 來源唯讀、clone
    落點 `<worktree>/%i` 已在 RWP 內。三份 unit 的 `ReadWritePaths` **逐位元不變**，
    monitor 對 Manager 的真子集不變式（#622）仍成立。
  - `PathLayout` 新增 `repo_source_root`／`gitconfig_of()`／`source_repo_slugs`
    （＋`with_source_repo_slugs()` 的 slug 形狀驗證），`with_job_segment()` 改用
    `dataclasses.replace`——逐欄位重建會讓每個新欄位靜默掉回預設值。

  新增 `tests/test_trust_root_repo_source_tree_623.py`（45 測試：兩個 job 帳號零寫入、
  Manager／monitor 唯讀、traverse 鏈完整（沿用 #624 的 `unreachable_hops()`）、
  `.gitconfig` fail-closed 與 CLI 兩條注入管道）；runbook 補第 2c 步、spec §R1 補裁決段。
