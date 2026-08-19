### Fixed

- **per-job clone 跨 owner，git 的 dubious-ownership 擋死 builder（#712）。**
  `#710`／PR #711 把工作區 ACL 補上之後，builder job **真的跑起來了**（`in_flight`
  約 12 分鐘），然後死在 git 自己那一層：

  ```
  fatal: detected dubious ownership in repository at '/var/lib/cortex/worktree/wf-…'
  fatal: Need a repository to create a bundle.
  ```

  **檔案系統層是通的**（實機 `getfacl` 逐字 `user:cortex-builder:rwx`／`mask::rwx`）
  ——擋住的是 **owner 判準，不是權限判準**。clone 的 owner 必然是 Manager
  （交出去要 `CAP_CHOWN`，Manager unit 帶 `CapabilityBoundingSet=`；`#710` 已論證），
  因此 git 必然看到跨 owner。
- **修法：逐 job 由 Manager 算出那一格，隨 spec 的 env 下去**
  （`GIT_CONFIG_COUNT=1`／`GIT_CONFIG_KEY_0=safe.directory`／`GIT_CONFIG_VALUE_0=<這一格>`，
  `job_runner.git_workspace_trust_env()`，就在 `PATH`／`HOME` 旁邊）。
  **`GIT_CONFIG_*` 真的設得動 `safe.directory` 已實測**（0819，git 2.43.0）：那是與
  `git -c` 同級的 **command scope**，屬 git 的 *protected configuration*，
  `git config --list --show-scope` 逐字回報 `command	safe.directory=…`；
  `git status` 與 builder 真正會跑的 `git bundle create` 皆 rc=0，而**同一份 env 對別的
  repo 仍是 rc=128 `detected dubious ownership`**。
  值一律取 **physical path**——實測走 symlink 進去時，`safe.directory=<symlink 路徑>`
  **仍被拒**（git 比對的是 `getcwd()` 之後的真實路徑）。

### Changed

- **三個降權 principal 的 git 工作區信任由同一條規則導出**
  （`registry.JOB_GIT_WORKSPACE_TRUST`）。三者形狀**本來就不同**，規則講的就是那個不同
  （逐一查證後決定，不是照抄）：

  | principal | 工作區 | 誰建 ⇒ owner | git 放行 |
  | --- | --- | --- | --- |
  | `builder` | `<pool>/<job-id>`（per-job clone，`.git` 是真目錄） | Manager | **要**——本票原症狀 |
  | `reviewer`／`planner` | `.psc-review-worktrees/<…>`（`git worktree add --detach` 的 linked worktree） | Manager | **要**——同樣跨 owner |
  | `reviewer`／`planner` | `planning-scratch/<…>/cwd`（空目錄，**沒有 repo**） | Manager | 注入是無害的 no-op |
  | `gate` | `<gate-worktree>/<key>`（自己 `copytree` 出來，含 `.git`） | **gate 自己** | **不要**——零動作、零 env |

  規則**不是第二個各自為政的決定**：git 的判準只有「repo 的 owner 是不是當下這個
  uid」，而「誰建那一格」已經寫在 `#710` 的 `JOB_WORKSPACE_REACH` 上
  ⇒ `pool-owned-by-job` ⟺ `owned-by-job`，其餘兩種形態 ⟺ `per-job-env`。
  由**兩條 import 期斷言**強制：缺一格或與 `#710` 那張表矛盾 ⇒ `registry` 載不起來；
  靜態 `.gitconfig` 若被加上萬用字元或工作區路徑 ⇒ `permgen` 載不起來。
  「只修一格」因此在**結構上做不到**（先例：#698／#708／#710）。
- ⚠️ **`GIT_CONFIG_*` 只放行 `safe.directory` 一個鍵**（`job_runner.ALLOWED_GIT_CONFIG_KEYS`）。
  這條管道與 `git -c` 同級，`alias.*`／`core.fsmonitor` 經它進來**會執行外部命令**
  ——0819 實測：`GIT_CONFIG_KEY_1=alias.pwn` ＋ `GIT_CONFIG_VALUE_1='!echo …'` 之下
  `git pwn` **真的跑了那條命令**（本票有一條測試逐字驗這件事）。那正是三份 `.gitconfig`
  必須 root-owned 的理由，本管道不得成為它的繞法。守衛
  （`_reject_unsafe_git_config()`）**寫端與讀端共用同一支**：`build_job_env()`／
  `build_job_spec()` 與 `job_shim.load_spec()` 各跑一次，只在寫端自律等於相信一個
  Manager 帳號可寫的檔沒被動過手腳。判準含：只認 `COUNT`／`KEY_<i>`／`VALUE_<i>`、
  KEY／VALUE 成對齊全（git 對缺項會整支 `fatal: unable to parse command-line config`）、
  鍵名大小寫不敏感比對（git 的 config 鍵本身不區分大小寫）、值必須是絕對路徑且
  **不得是字面 `*`**。
- ⚠️ **同一扇門的另外五個把手一併關上**：`GIT_CONFIG`／`GIT_CONFIG_GLOBAL`／
  `GIT_CONFIG_SYSTEM`／`GIT_CONFIG_NOSYSTEM`／`GIT_CONFIG_PARAMETERS` 進
  `job_runner.DENIED_ENV_NAMES`。`GIT_CONFIG_GLOBAL` 會讓 root-owned 的
  `$HOME/.gitconfig` 整份失效，`GIT_CONFIG_PARAMETERS` 是 `git -c` 的序列化管道、
  **不受單鍵白名單約束**——放行了單鍵卻留著這五個等於白做。
- **放行綁死在這個 job 的那一格上**：`build_job_spec()` 斷言 env 放行的路徑**逐字等於**
  spec 的 `working_directory`（值由 Manager 算，job 改不了自己的 spec——#638／#639 的
  spool 模型）。指到別處的那一條既救不了這個 job，又擴大了放行面。
- **更正三則陳舊宣稱**（#696 的教訓：陳舊宣稱會**反向說謊**，本票是第三個實例）：
  - `builder-gitconfig` 的 `RunDependency` note 逐字寫著「per-job clone 的
    `safe.directory`」，而產生器實際只出**來源樹**那兩條——那則宣稱活了兩個月；
  - `reviewer-planner-gitconfig` 的 note 只寫「同 `builder-gitconfig`」，於是連同那則
    錯誤宣稱一起繼承了；
  - 兩份模板 unit 的工作區註解同樣寫著「跨擁有者 clone 由 `<gitconfig>` 的
    `safe.directory` 放行」。
  三則都改成實際成立的描述，並由規則表逐 principal 產生 unit 的 git 信任那一段
  （三份必然不同）。`build_account_gitconfig()` 的 docstring 與**產物內容本身**也加上
  涵蓋範圍的明文邊界。

### Added

- **反向不變式的實機探針**：`python3 -m paulsha_cortex.trust_root git-trust-probe`
  （`permgen.build_job_git_trust_probe`）。三個方向缺一不可：**缺陷基線**（零額外 env、
  真實加固面下 git 必須擋住——這一步同時證明靜態 `.gitconfig` 沒有蓋到這一格）、
  **正向**（`git status` ＋ `git bundle create` 成功）、**反向**（同一個 job、同一份 env，
  在**別的 job 的工作區**裡仍然失敗）。
  兩格工作區都由**真實 provisioning** 產生（`seams.ScriptWorktreeCreator` ＋
  `job_runner.ensure_workspace_reachable()`），**不手工前置**（#645／#709）。
  ⚠️ 基線走 `psc_run_under`／`unit_replica_properties()` 全量導出（D13：**不得自組
  `--property=`、不得自帶 `--setenv=PATH=`**，由 `path_probe_env_injections()` 機械釘住）；
  **正向／反向兩步刻意走真實派工**（真 spec ＋ `systemctl start --wait`）——#709／#687
  記過 `psc_run_under` 複製的是**加固面**、不是派工路徑，而本票要驗的東西住在 spec 的
  env 裡，用 `--setenv=` 自己塞進去只會量到「我塞的東西生效了」。
- **單元層的真 git 反向不變式**（`tests/test_per_job_git_safe_directory_712.py`）：以 git
  自己的 `GIT_TEST_ASSUME_DIFFERENT_OWNER=1`（`ensure_valid_ownership()` 的第一個條件）
  在單 UID 的 CI 上複現跨 owner 的**判定路徑**，逐條驗基線失敗／自己的工作區成功／
  **別的 job 的工作區失敗**／symlink 路徑不算數／linked worktree 蓋不到／alias 真的會
  執行。真正需要第二個 UID 的那一維留一條**具名 `@pytest.mark.skip`**，理由指向實機探針。
- runbook 第 **4e-2f** 步：形狀對照表、靜態檔沒被加寬的檢查、缺陷基線、反向不變式探針、
  真實派工 smoke（含「spec 裡那三個鍵在不在、值等不等於 `working_directory`」），以及
  「gate 那一格真的是空的」——把「不需要」與「忘了做」分開。
