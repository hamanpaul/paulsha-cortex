# lazy-repo-root

### Fixed
- **#633 / trust-root Phase 2b：`ScriptWorktreeCreator` 的 repo 解析改 lazy——Manager 不再
  「因為少一個 env 變數」啟動即崩**（Closes #633）

  **實機現場**：#612／#630 把 `paths.repo_root()` 改成未宣告 `PSC_REPO_ROOT` 即拋
  `RepoRootUnresolvedError`——方向正確，但它命中的位置在**啟動路徑**上：

  ```
  manager_daemon.run_loop → ensure_dispatcher()
                          → Dispatcher(…, ScriptWorktreeCreator())
  seams.ScriptWorktreeCreator.__init__ → paths.repo_root()
  paths.repo_root                      → raise RepoRootUnresolvedError
  ```

  `ScriptWorktreeCreator()` 在 **`run_loop` 建 dispatcher 當下**就實體化，因此 Phase 2b
  的 EnvironmentFile 少了 `PSC_REPO_ROOT`（#623 缺口 2）時，後果不是「派不了工」而是
  **Manager 啟動即崩**，`Restart=on-failure` 再把它推進 crash-loop（實機 `NRestarts`
  連跳 7 次）。一台什麼都做不了、也什麼都不告訴 operator 的機器。

  **修法：只改時機，不改性質。** `__init__` 不再解析 repo／worktree pool，改由
  `_repo` / `_wt_root` 兩個 property 在**第一次真正要用時**解析並 memoize（memoize 是
  刻意的：舊實作在建構子解析一次、其後凍結，全部既有呼叫端都建立在「同一個 creator
  永遠對同一棵樹動手」之上）。於是

  - 沒有宣告目標 repo 的 Manager **起得來**，tick／monitor／狀態回報／降級運轉照常
    ——與 `PSC_DEGRADED_OPERATION` 的精神一致：能做的繼續做；
  - 第一次 `create()`（＝真的要在磁碟上開一棵樹）仍 `RepoRootUnresolvedError` **原樣**
    拋出、訊息逐字不變，只是出現在**派工當下**而不是啟動當下——那也正是 operator 看
    得懂它的時刻；
  - fail-closed 一個位元組都沒放寬：沒有新增任何 cwd 退路、沒有吞例外，且 fail-closed
    發生在**任何磁碟動作之前**（`create()` 第一件事就是算 `_wt_root`，因此不會先
    `mkdir` 再後悔）。

  唯一新增的「不拋」入口是 `ScriptWorktreeCreator.anchored_at(root)`：它回的是 `False`
  （＝這個 creator 不能拿來對 `root` 派工），不是一個猜出來的路徑。
  `manager._dispatch_workflow_card()` 的 build 分支改問它，取代原本直接讀
  `creator.repo_root` 再比對——lazy 化之後「repo 尚未解析且環境沒宣告」是 dispatcher 上
  一個合法的 creator 狀態，直接讀會讓例外從一句比較裡漏出去。語意不變：錨定的不是本
  run 的 `workspace_root` 就換一個錨定正確的。#646 的必填 `job_id`、#656／#659 provision
  Manager-owned 樹的兩個呼叫端都顯式帶 `repo=`／`wt_root=`，完全不受影響。

  **測試**：新增 `tests/test_lazy_repo_root_633.py`（9 條）——建構子／`Dispatcher`／
  整條 `run_loop` 啟動路徑在未宣告 `PSC_REPO_ROOT` 時皆不拋；`create()` 與 `repo_root`
  仍 fail-closed 且訊息可操作（含 `PSC_REPO_ROOT`／`cwd`／`allow_cwd` 三個關鍵字）；
  磁碟上不留任何 worktree pool；顯式路徑完全不碰 `paths`；解析恰好一次且不隨 env 漂移。
  突變驗證（把解析搬回建構子）9 條中 8 條轉紅。`tests/test_repo_root_fail_closed_612.py`
  的對應條目改為釘死 `create()` 而非建構子。

### Changed
- **#633：trust-root Phase 2b runbook——EnvironmentFile 的展開慣用法、模板引號，以及兩條
  ACL 警語**（`docs/superpowers/runbooks/trust-root-phase2b-setup.md`）

  - **`env $(grep -v '^#' <envfile> | xargs)` 全數改掉（10 處）**。`$(… | xargs)` 依
    **空白**切詞，因此任何值含空格的變數都會被拆成多個參數。實機補上
    `PSC_GATE_CMD_PYTEST=python3 -m pytest -q` 之後，那 10 條驗證指令全數變成
    `env: ‘-m’: No such file or directory`（rc=127）；改用未加引號的 shell source 也
    一樣（shell 把 `-m` 當命令）。**systemd 自己沒問題**——`EnvironmentFile` 把 `=`
    之後整段當值——壞的只有 runbook 的驗證指令，而 `PSC_GATE_CMD_*` 這族**天生含空格**
    （它們是命令列），不是邊角案例。一律改為
    `sh -c 'set -a; . /opt/cortex/etc/cortex-manager.env; set +a; …'`；帶 heredoc 的兩處
    利用「`sh -c` 的腳本來自 argv、stdin 原樣留給 `python -`」保持原形狀。
  - **第 4b 的 env 檔模板：值一律加引號，並補上八個操作變數**（`PSC_REPO_ROOT`／
    `PSC_REPO_IDENTITY`／`PSC_MANAGER_EXECUTOR`／`PSC_GATE_CMD_PYTEST`／
    `PSC_GATE_TIMEOUT`／`PSC_MANAGER_INTERVAL_SECONDS`／`PSC_MANAGER_GITHUB_INTERVAL_MS`）。
    引號讓同一份檔案對 systemd、對 `sh` 的 `.`、對驗證指令三邊都讀得對。
    `PSC_PREFLIGHT_CMD` 刻意仍未補——舊值在 `ProtectHome=yes` 下不可達，搬到哪裡是
    #623 的部署樹問題，未決之前留白勝過填一個跑不起來的值。新增一條
    「這份 env 檔 shell 也 source 得動」的驗證，以及第 7b 的 **F2b**
    （`paths.repo_root()` 真的指向來源樹）——lazy 化之後 `systemctl is-active` 全綠
    **不代表**派得了工，這條才是判準。
  - **警語 1（5-3a）：改變 ACL 結構時，舊的 default ACL 會靜默地跟著新物件走。**
    實機案例——`job-specs` 原本是 builder 專用、容器帶著
    `default:user:cortex-builder:r-x`；#657（PR #660）改成 per-principal 三格之後，新建
    的三個子目錄**全部繼承了那一條**，於是 builder 一開始就讀得到 reviewer 與 gate 的
    格，正是 per-principal 要防的事。處置是改結構前先 `setfacl -k <容器>` 清掉容器層過時
    的 default ACL（`-k` 只動 default、不動 access，容器自己的 traverse 不受影響），再
    重跑權限計畫。另補一條**機械判準**：每格具名條目 `foreign=0`、`#effective:` 註記為 0
    ——肉眼掃 `getfacl` 會漏掉混在裡面的那一條。
  - **警語 2（2b）：升級既有部署時重跑也要守 scaffold → permissions 的順序。**
    反過來的話 scaffold 的 `install -d -m` 對**既存**目錄會重設 mode，而那次 `chmod`
    會重寫 ACL mask，讓權限 script 剛套好的具名條目變成 `#effective:---`——ACL 還在、
    `getfacl` 逐條看得到，實際權限是零。與 5-3a 的「mask 陷阱」同一個成因，只是觸發點
    從「手動補 ACL」換成「兩份 script 跑反了」。兩條警語的驗收判準一致：看 `mask::`
    與 `#effective:`，**不是**「有沒有那條 ACL」。
