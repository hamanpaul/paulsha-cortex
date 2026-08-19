### Fixed

- **per-job clone 建好之後沒有交給 job 帳號——builder job `cd` 不進自己的工作區（#710）。**
  per-job clone 是 **Manager** 用 `git clone` 建的 ⇒ `cortex-manager:cortex-manager 700`、
  **零具名 ACL**；模板 unit 的 `ReadWritePaths=<pool>/%i` 在 **mount 層**放行，**DAC 層
  擋死**（兩層要同時成立）。`#708`／PR #709 修好 log 之後，`shim-error.json` 第一次交出
  這條逐字原因：`[Errno 13] Permission denied: '/var/lib/cortex/worktree/wf-…'`。
  `cortex-reviewer-job@.service` 的註解宣稱「整個 clone 由本 job 帳號擁有」，而**全
  `paulsha_cortex/coordinator/` 零個 `chown`／`shutil.chown`／`os.chown`**——而且 Manager
  **結構上做不到**：`chown` 給另一個使用者需要 `CAP_CHOWN`，Manager unit 帶
  `CapabilityBoundingSet=`（空）。**這不是漏寫一行，是方案與降權模型衝突。**

### Changed

- **三個降權 principal 的工作區可達性由同一條規則導出**（`registry.JOB_WORKSPACE_REACH`）。
  三者現況**本來就不同**，規則講的就是那個不同（各自查證後決定形狀）：

  | principal | 工作區 | 誰建 | 可達性 |
  | --- | --- | --- | --- |
  | `builder` | `<pool>/<job-id>`（per-job clone） | Manager（`seams.ScriptWorktreeCreator`） | **本票補上的**具名 ACL（`setfacl -R -m u:<帳號>:rwX` ＋ default `rwx`） |
  | `reviewer`／`planner` | `.psc-review-worktrees/<…>`、`planning-scratch/<…>/cwd` | Manager | pool 根既有的 default ACL 繼承 ⇒ **零動作** |
  | `gate` | `<gate-worktree>/<key>` | **gate 自己**（`copytree`） | pool 根 owner 就是 `cortex-gate` ⇒ **零動作、零 ACL** |

  由**兩條 import 期斷言**強制：缺一格 `registry` 載不起來
  （`_assert_every_downgraded_principal_has_a_workspace_reach`）、宣告的機制與權限計畫
  對不上則 `permgen` 載不起來（`_assert_job_workspace_reach_matches_the_plan`）。
  「只修一格」因此在**結構上做不到**（先例：#698 的 `EXECUTOR_ENFORCEMENT_LEAVES`、
  #708 的 `JOB_LOG_SPOOLS`）。
- ⚠️ **授權只下在 per-job 那一格，不下在 pool 根**——pool 根是三個 job 帳號共用的容器
  （`0701 cortex-manager`），在它身上下 default ACL 會讓**每個** job 帳號進得去**每個**
  job 的目錄，裁決 10-2 的 per-job 隔離當場歸零。三個角度各釘一次：permgen 的 import
  期斷言（pool 根上出現任何 job 帳號的 default ACL ⇒ 模組載不起來，含突變驗證）、
  執行期把 pool 根當引數時 **fail-closed**、以及一條真的 ACL 樹上「job B 進不去 job A
  那一格」的實測。
- ⚠️ **ACL 遞迴套用，且 `chmod` 一律排在 `setfacl` 之前**。樹裡每個 inode 都由 Manager
  以 `UMask=0077` 建立，只在樹根下一條 ACL 的話 job 進得去卻讀不到裡面任何東西；而任何
  後續 `chmod` 都會重寫 ACL mask、讓具名條目**靜默失效**。判準一律是 `getfacl` 的
  `mask::` 與 `#effective:`，不是「ACL 行存在」（runbook 4e-2b）——本票以一棵真的 ACL
  樹把這條陷阱本身測出來（`chmod` 之後 ACL 行還在、有效權限是零）。
- **`repo-worktree` 的登記表形態與 permgen 的權限計畫改為與實機一致**：owner＝Manager
  ＋ job 具名 ACL（原本宣告的 `chown cortex-builder` 是一條 Manager 執行不了的指令）。
  `#629` 宣告的 `cortex-gate:rX` 由**同一次** per-job setfacl 一起落地——在本票之前它與
  builder 那條一樣只存在於註解裡。
- **更正三份模板 unit 的工作區註解**（#696 的教訓：陳舊的宣稱會反向說謊）。那一段原本是
  三份 unit 逐字共用的一塊硬寫死註解，內容是 builder 的故事——對 reviewer 與 gate
  **每一個子句都是假的**（工作區不在 pool 底下、也不在自己的 `ReadWritePaths=` 裡）。
  現在由規則表逐 principal 產生，三份必然不同，且各自等於它那一列宣告的機制；builder
  那一段把舊句點名並說明它為什麼結構上做不到（`CAP_CHOWN`／`CapabilityBoundingSet`）。
  `registry` 內 `repo-worktree`／`dispatch-worktree-pool`／`gate-worktree-pool` 三則
  同型的陳舊 note 一併更正。

### Added

- **`setfacl` 進入窮舉盤點**（`permgen.SYSTEM_PROGRAMS` ＋ `RUN_EXTERNAL_DEPENDENCIES`，
  #666／PR #671 的雙向封閉）。Manager 以名字解析它（與 `systemctl` 同一條），解不到即
  **fail-closed** 並在訊息裡指出它由發行版的 `acl` 套件提供——0818 trust-root Phase 2b
  的三個部署陷阱之一就是這個套件缺席。
- **反向不變式的實機探針**：`python3 -m paulsha_cortex.trust_root workspace-probe`
  （`permgen.build_job_workspace_probe`）。三個 principal 各一段，以**零額外 env**、
  真實模板 unit 的加固面正向斷言「`cd` 得進自己的工作區並做得到該做的事」，反向斷言
  「別的 job 帳號進不去 builder 那一格」，並以 `getfacl` 的 `mask::`／`#effective:`
  判準驗 ACL 沒有被 `chmod` 壓掉。加固面複本一律走 `psc_run_under`／
  `unit_replica_properties()` 全量導出（D13：**不得自組 `--property=`、不得自帶
  `--setenv=PATH=`**，由 `path_probe_env_injections()` 機械釘住）；工作區由
  **真實 provisioning** 產生（`seams.ScriptWorktreeCreator` ＋
  `job_runner.ensure_workspace_reachable()`），**不手工前置**（#645 逐字記錄過手工前置物
  會把 bug 繞過去；#709 記了「`psc_run_under` 證明不了派工路徑」）。
- runbook 第 **4e-2e** 步：形狀對照表、`acl` 套件檢查、產生器輸出逐字對照、pool 根零
  default ACL、`getfacl` 的 mask 判準（含樹**裡面**那一層）、per-job 隔離反向驗證、
  反向不變式、真實派工 smoke，以及回收面的已知邊界。

### 已知邊界（記錄，不是本票要解的）

- builder 終於寫得出東西之後，工作區裡會出現 **builder 擁有**的 inode。default ACL 讓
  `cortex-gate` 仍讀得到它們（POSIX：目錄帶 default ACL 時 **umask 不生效**，實測確認），
  但 **Manager 沒有**那些 inode 上的任何條目（#641 收掉了它的 reader 面）⇒
  `gc`／`worktree_reclaim` 的 `rmtree` 走不進 builder 新建的子目錄，工作區會殘留。
  這在「clone 由 job 帳號擁有」的原設計下**更嚴重**（Manager 連樹根都進不去），因此
  不是本票造成的退步；補 Manager 的 default ACL 會把 #641 關掉的提權面整條打開，故
  **刻意不補**，回收面的處置另立一票。
- Manager 是 per-job 那一格的 **owner**，owner 位給的存取收不掉。#641 收掉的是「登記表
  **主動授予** Manager 的跨帳號 ACL」，那條仍然不存在；但「Manager 進不去 job 樹」在
  Manager 必須回收工作區的前提下**結構上不成立**。`verification` 那條提權路徑因此由
  fail-closed（`candidate-worktree-unreadable-pending-gate-identity`）與 #629 的第三
  執行身分擋住，不由這棵樹的權限擋住——這是實機 0817 起就成立的狀態，本票只是讓登記表
  與產生器停止宣稱相反的事。
