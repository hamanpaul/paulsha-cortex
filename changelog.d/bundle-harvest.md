### Changed
- **#623 / trust-root Phase 2b：成果回收由「Manager 伸手進 builder 的 clone fetch」
  改為 **git bundle ＋ append-only spool**（coordinator 側）**（Refs #623）

  **為什麼非改不可**：#634 落地的回收做法是
  `git -C <來源樹> fetch --no-tags <builder 的 clone> refs/heads/<b>:refs/heads/<b>`。
  operator 0817 實機驗證它在三分下**行不通**，兩個獨立原因：

  1. **Manager 走不進 builder 的樹**——job 的 clone 是 builder-owned `0700`，
     `git -C <clone> …` 直接 `fatal: cannot change to '…': Permission denied`。
  2. **per-job 路徑無法用一條設定涵蓋**——就算補了父鏈 traverse，Manager 對每個 job
     的 clone 還需要跨擁有者的 `safe.directory`，而實測 git 2.43 **不吃路徑 glob**，
     只認逐字相等或字面 `*`。

  **改法**：builder 在自己的 clone 產出 bundle → 寫進 Manager-owned 的 append-only
  spool → Manager 從**那個檔案** fetch。關鍵在 Manager 讀的是一個**普通檔**而不是一個
  repo：dubious-ownership 與 traverse 兩個問題同時消失，且 Manager 全程不需要（也不
  應該）存取 builder 的樹。operator 實機驗證：回收 `rc=0`、commit 與 diff 都正確落地，
  期間 `ls <clone>` 仍 `Permission denied`；builder 也讀不到 spool 裡別人的東西。

  **路徑契約**：`<coordinator_root>/commit-spool/<job-id>/commits.bundle`，形態完全
  比照既有的 `review-verdict-spool`——容器 owner＝`cortex-manager` 0700、producer 以
  **`wx` 無 `r`** 的 per-account ACL、per-job 目錄由 Manager 在 dispatch 當下建立
  （`prepare_commit_spool`）、成果落地後轉唯讀（`seal_commit_spool` 把目錄收成
  `0500`——bundle 是 builder-owned，Manager `chmod` 不了它，但它是目錄的 owner，
  收掉 `w` 之後那一格就定版，POSIX ACL 的 mask 也一併收窄）。登記表資產與 OS 權限由
  #636（已 merge）在 `trust_root/` 定義，路徑契約的權威 resolver 是
  `config/paths.py:commit_spool_root()`；本 PR 只做 coordinator 側，
  `job_workspace.commit_spool_root()` 逐字委派給它，只多接受一個**顯式**的
  coordinator root（`manager` 與 `verification` 都有這個參數，一律回頭讀 env 會讓
  同一個 job 的 dispatch 與 harvest 指到兩棵不同的樹）。

  **bundle 不是證據，是搬運**：#628 把 gate ledger 與 exit sentinel 的作者收斂到
  Manager，理由是「被驗方不得在自己的進程裡產生自己的驗收證據」。bundle 不適用那條
  ——它不宣告任何結論，只把 commit 從一個 object store 搬到另一個。採信仍全由 Manager
  判斷：canonical lane 的回收掛在 `_verify_build_candidate_transition` **之後**，且回收
  後來源樹的 branch **必須恰等於已採信的 candidate**，對不上即 fail-closed（沿用 #540
  的 acceptance chain）。bundle 內容由 builder 掌控因此不新增任何採信面，它能造成的
  最壞情況就是讓回收失敗。

  **bundle 在哪個時點產生**：Manager 組出的 wrapper script 裡，模型 argv 之後、**exit
  sentinel 之前**（`launcher.build_wrapper_script` 的 `commit_bundle=`）。段序是
  模型 → 存 `$?` → bundle → sentinel → gate → `exit "$rc"`。兩個理由：sentinel 一出現
  Manager 隨時可能在下一個 tick 開始回收，bundle 必須先落地；而降權模式下 unit 的
  exit code 就是這支 script 的 exit code（#604 的記帳 shell 記的正是它），多接一段
  之後若不還原 `$?`，模型的成敗會被 bundle 步驟污染。bundle 段用 `git` 而不是像 gate
  那樣呼叫 python module——降權模式下 builder 未必讀得到 Manager 的 repo root
  （`ProtectHome=yes` 之後 `/home` 整個不可見），`PYTHONPATH=<repo>` 那條路在那裡不
  成立。`.part` → `chmod 0644` → `mv` 三步以 `&&` 串接：spool 裡看得見的
  `commits.bundle` 恆為完整檔，而 `chmod` 讓降權 unit 常見的 `UMask=0077` 不會產生一份
  Manager 讀不到的成果。

  **`^<base>` 怎麼推導**：provisioning（`seams.ScriptWorktreeCreator`）在 clone 內
  `update-ref refs/cortex/base <exact_base>`，wrapper 以 `^refs/cortex/base` 收斂 bundle
  範圍。`exact_base` 是**來源 repo 自己** `rev-parse --verify` 出來的 commit，因此
  「來源樹一定有 bundle 的 prerequisite」這條性質由 provisioning 這個**單一推導點**對
  每一條 lane 一致成立（lane 之間唯一的差別是傳給 `create()` 的 `base_sha`，而它在同
  一個函式裡被同一次 `rev-parse --verify` 收斂）。base pin 放在 clone 內而不是寫死在
  wrapper 裡，是因為產 bundle 的是 builder：它讀得到自己的 clone，卻讀不到 spool
  （ACL 是 `wx` 無 `r`）也讀不到 Manager 的任何狀態。

  **不完整 bundle 的處理**：`git fetch` 對缺 prerequisite 的 bundle 只吐
  `error: Repository lacks these prerequisite commits:` 加一串裸 SHA，看不出下一步。
  `harvest_branch()` 因此逐類包一層可操作說明——bundle 缺席（列出兩種成因與該去哪看
  逐字原因）、prerequisite 缺席（指出處置是**重新 provision**，而不是放寬 refspec）、
  bundle 帶的是別的 branch（指出用 `git bundle list-heads` 確認，不得改用其他 ref
  回收）、非 fast-forward（訊息逐字保留）。四類全部 fail-closed，沒有任何一條退回讀
  clone。

  **bundle 的保留策略**：成功回收後**保留並封存**，不刪除。#634 加的
  `refs/cortex/reclaimed/**` 封存機制是為了讓 `rmtree` 掉 clone 時不銷毀未回收的
  commit，而它的實作本身要 `git -C <clone>` ——在三分下同樣不可行。bundle 正是那個
  機制的替代品：它是那些 commit 在 Manager-owned 樹裡的副本，且取得它完全不需要碰
  builder 的樹。`archive_workspace_head()` 因此原地保留給升級前既存的 linked
  worktree，clone 形狀改以 bundle 為證據面。（把 `worktree_reclaim` 也改成優先讀
  bundle 需要「工作區路徑 ↔ job id」的對應，目前不存在，列為後續票。）

  **spool key 的推導只有一條規則**：`Path(job["log_path"]).stem` ——那正是
  `launcher.launch()` 收到的 `slice_id`，同時決定 exit sentinel 與 gate ledger 的落點。
  canonical lane 的 launch key 是 job_id、slice lane 的是 slice_id，兩條 lane 若各自
  在回收端猜自己的 key，任何一邊改名都會退化成「找不到 spool → 靜默不回收」——最壞的
  失敗形態。同理，「這個 job 要不要回收」的判準改為 **Manager-owned spool 那一格在
  不在**，不再是「工作區是不是 clone」：後者要讀 `<clone>/.git/` 底下的標記檔，三分下
  Manager 讀不到、判準恆為 False。`job_workspace` 的形狀判定同時收斂了
  `PermissionError`（回 False 而不是讓一個 tick 整個掛掉）。

  **`direct` 模式相容性**：沿用 #634「以工作區自己的形狀判斷、不依 `PSC_JOB_RUNNER`
  分支」的原則——spool 授權在 dispatch 當下決定，兩種模式走完全相同的路徑。判準是
  **persona**（reviewer／planner 不產生 commit，不給 spool），與 `_should_run_gates`／
  `_downgraded_mode` 既有的 persona 分支對齊。`commit_bundle=None` 時
  `build_wrapper_script` 的輸出**逐字**與改動前相同。

  新增 `tests/test_bundle_commit_harvest_623.py`（30 測試，全部以真 git repo 驗證）：
  base 錨點單一推導、bundle 產生→回收→內容正確、**不變式**（把 clone `chmod 000`
  重現實機的 `Permission denied`，整條回收路徑照常完成——已做突變驗證：在回收路徑裡
  加一次 clone 存取，這條當場紅）、四類 fail-closed 與訊息可操作、spool 的 pre-seed
  與 seal、wrapper 段序與 exit code（真的跑一次 bash）、降權形狀、`direct` 零回歸。
  另修正三個 trust-root launch 測試檔：它們以 `clear=True` 重建 environ、把 conftest
  的 `PSC_AGENTS_ROOT` 保護一併清掉，因此顯式帶上 per-process 暫存根（否則 launch 會
  在 operator 的真實 `$HOME` 底下建 spool——正是 conftest #303 註記要防的事）。全套
  `python3 -m pytest tests/ -q`：3674 passed，零回歸。
