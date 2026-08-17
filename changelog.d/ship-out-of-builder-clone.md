# Issue #653：ship 段全程在 builder 的 clone 裡動手——#641 收掉讀取授權後，降權模式下第一個 `git -C` 就 `Permission denied`

## 問題（#654 已查證，與 #649 票面上的假設不同）

`openspec-archive`／`policy-commit` **不是降權派工的對象**：兩張卡的 persona 是
`manager`，而 `manager._dispatch_workflow_card()` 對 `current_phase == "ship"` 一律回
`None`——它們不經 launcher、不 spawn job，由 Manager 自己在 `work_bridge.py` 內以
`cortex-manager` 身分同步執行。因此**沒有 template unit、沒有 `ReadWritePaths=<pool>/%i`、
沒有 `226/NAMESPACE`**。

真正的阻擋物是**權限**：ship 段全程在 `_builder_binding()` 交回來的 **builder 的
clone** 裡動手——

| 位置 | 動作 |
|---|---|
| `_builder_binding()` | `Path(worktree).resolve(strict=True)` |
| `_remove_canonical_untracked_reports()` | 讀／刪工作區內的 canonical report |
| `work_actions._validate_local_archive_inputs()` ＋ `openspec archive` | `cwd=<那棵樹>` |
| `_commit_archive_and_require_reverification()` | `git -C <那棵樹> diff/add/commit/rev-parse` |
| `_run_exact_candidate_preflight()` | preflight 在那棵樹跑 |
| `_push_exact_candidate()` | `git -C <那棵樹> ls-remote/push` |
| `work_actions._ship_action(repo_root=…)` | 連測試都在那棵樹跑 |

Phase 2b 三分下那棵樹是 `cortex-builder` 擁有的 `0700` clone，而 **#641 已把登記表裡
Manager 對 job 工作樹殘留的讀取授權全部收掉**（runbook 第 2 步的稽核 5b 要求
`/var/lib/cortex/worktree/` 底下零 `setfacl`）⇒ 降權模式下 ship phase 會在**第一個
`git -C`** 就 `Permission denied`。本 PR 的突變驗證逐字重現了這句話（見「測試」）。

**不得把那條 ACL 加回來**：#644 的論證是那條授權唯一的消費端（在 builder 掌控的樹裡
執行命令）本身就是一條提權路徑；加回來等於把它復活。

## 修法：ship 段 provision 一棵自己的 Manager-owned clone

`work_bridge._manager_ship_workspace()`：以 `run.candidate_head` 為 base，用
`seams.ScriptWorktreeCreator` 在**來源樹**上 clone 一份。來源樹
`/var/lib/cortex/repos/<slug>` 是 `cortex-manager` 擁有且可寫（0817 裁決），Manager 對
自己 clone 出來的樹自然是 owner——commit／preflight／push 全部沒有權限問題，也**不需要**
任何指向 job 工作樹的 ACL。creator 的兩道既有守衛在這條 lane 上剛好就是要的：

- `rev-parse --verify <candidate>^{commit}`：來源樹必須已經有這個 commit——那正是
  #654 的回收通道所保證的不變式。回收沒走完就 provision 不起來，而不是在很遠的地方
  以看不懂的訊息炸開。
- `merge-base --is-ancestor <branch> <candidate>`：delivery branch 不得帶著 candidate
  以外的 commit。

`_builder_binding()` **只回 delivery branch**，不再回（也不再 `resolve()`）那個 job 的
工作區：它真正不可取代的職責是**採信鏈**——foreign review 指名的那一個 builder（或
post-archive 的 manager archive）job 必須是 `subject_head == candidate` 的那一筆。選 job
的那一段**一個位元組沒改**。

### 工作區的識別與生命週期

識別 `_ship_workspace_id()` 穩定於 **(run, candidate)**（形如
`wf-<run 摘要>-ship-<candidate 前綴>`，目錄名仍由唯一推導點 `job_workspace.job_segment()`
導出）。兩個直接後果：

- **同一個 candidate 的多次 tick 共用同一棵樹**。ship phase 會被 tick 很多次（等
  preflight、等 PR、等 copilot、等 merge），每次都 clone 一份 35MB 是白燒。
- **candidate 前進就換一棵**，前一棵原地留著——它正是 `_record_manager_ship_job()` 記在
  archive 卡上的 `worktree`，post-archive 的 verify／review 卡仍以
  `builder_jobs[-1]["worktree"]` 當 candidate 樹（`manager._dispatch_workflow_card()`）。
  **ship 段因此不自己刪樹**，回收交給 `cortex work gc`，與 build 卡的 clone 同一套。

### `archive-applied-needs-commit` 的重入路徑（#653 明載必須一併處理）

`_ship_action()` 可能在工作區裡把 archive 套用完但沒 commit，`work_bridge` 才回頭呼叫
`_commit_archive_and_require_reverification()`。票上點出：若那時才另開一棵乾淨的 clone，
已套用但未 commit 的異動會不見 ⇒ `changed` 為空 ⇒
`archive diff escaped strict OpenSpec/docs/changelog allowlist`。

處置分兩層，取的是票上兩個選項中的「**在新樹裡重跑 archive**」：

1. **同一次 `validate()` 內**兩件事本來就在同一棵樹——工作區在 `validate()` 開頭
   provision 一次，`_ship_action()` 與後續的 commit 拿到的是同一個 `worktree`。這是
   結構性的，不靠約定。
2. **跨 tick**（上一次崩在中間）：`_manager_ship_workspace()` 重用既有樹之前一律
   `checkout -f`／`reset --hard <candidate>`／`clean -ffdx` 打回 pristine，再以
   `_require_pristine_ship_workspace()` 驗證 branch／HEAD／乾淨三條。套用 archive 對同一
   個 candidate 是完全可重現的確定性動作，重跑一次比帶著來歷不明的 dirty 檔往下走安全
   得多——而且讓「崩在中間」與「從沒跑過」收斂成同一個狀態。已 commit 但**還沒回收**的
   archive commit 同樣被丟掉：回收是採信發生的唯一時點，沒回收就沒有任何下游依賴它。

### `_remove_canonical_untracked_reports()` 移除

那個函式的用意是「reviewer 發佈在 candidate 樹裡的未追蹤 report 不得弄髒要 commit 的
exact candidate」。它讀／刪的正是 **builder 的 clone**——本票要消滅的東西。新模型下 ship
段拿到的是一棵 pristine clone，未追蹤檔根本不會被 clone 過去，「候選樹被 report 弄髒」
在**結構上**不再可能發生。因此改為由開工前的
`_require_pristine_ship_workspace()`（branch 對、HEAD ＝ candidate、`status --porcelain`
為空）承擔同一個保證，原函式與它的 `report-cleanup` evidence 產生路徑一併移除。

`manager._workflow_report_cleanup_allows_missing()` **保留**為向後相容的容忍面（升級當下
正在進行、已寫過該 evidence 的 run 仍要走得完），並補上 docstring 說明它已無產生端；沒有
那份 evidence 時仍一律 fail-closed。副作用是 report 從此留在 verify／review 卡自己的
`workflow_repo_root` 裡，`_read_job_workflow_evidence()` 的 artifact 校驗因此**不再**需要
走容忍路徑——比刪掉它更不脆弱。

## 紅線遵守

- **沒有**把 Manager 對 job 工作樹的 ACL 加回來（#644）；ship 的樹是 Manager 自己 clone
  的，`getfacl` 下不該有任何具名條目，稽核 5b 的「零 `setfacl`」仍然成立。
- **沒有** `--reference`／`--shared`／任何把 object store 接回共用的優化（#623 判定共用
  object store 與三分隔離互斥）。
- **沒有** `git -C <來源樹> fetch <某棵 job 的 clone>`。archive commit 回到來源樹走的是
  #654 的 bundle ＋ append-only spool，consumer 仍是全 repo 唯一的
  `job_workspace.harvest_branch()`——本 PR 對回收通道**一個位元組都沒改**。

## 測試

新增 `tests/test_ship_out_of_builder_clone_653.py`（7 條 ＋ 1 條 skip，全部跑正式路徑：
真 git repo、真 per-job clone、真 `build_production_ship_validator()`、真
`ScriptWorktreeCreator`、真 `_run_exact_candidate_preflight()`）。harness 取自
`test_preflight_closeout_order`——repo 內唯一一份真的把 production ship validator 端到端
跑起來的 fixture，複製一份只會讓兩邊漂移。

- **本票的全部價值**：把 builder 的 clone `chmod 000`（＝實機上 `0700 cortex-builder` 對
  Manager 的樣子），ship 段仍完整跑完 local closeout、archive commit 回收進來源樹，而那
  棵 clone 的 active change 與 HEAD 一個位元組沒動。形狀沿用 #637 的
  `test_manager_never_touches_the_builder_clone_while_harvesting`。
- **工作區身分**：pool 底下、`is_job_clone()` 為真、標記檔的 `branch`／`base` 對得上、
  `.git` 是目錄（不得退回 linked worktree），且 `openspec archive` 只在那棵樹裡被套用過
  （假 runner 改成以呼叫端給的 `cwd` 為準，那才是真 `openspec archive` 的行為）。
- **重用 ＋ pristine**：同一個 candidate 連續兩次進入拿到同一棵樹（標記檔的
  `created_at` 沒變 ⇒ 沒有重新 clone），而上一輪留下的「已套用未 commit 的 archive ＋
  雜物」被打回原狀。
- **`archive-applied-needs-commit` 重入**：把 run 推到「archive 已宣告完成、PR 已綁定」
  ——那是 `_ship_action()` 回這個 action 的唯一入口——驗證 archive 套用與 commit 落在
  **同一棵** Manager-owned 樹裡，回收成立，且 builder 的 clone 全程沒動。
- **`openspec-archive` → `policy-commit` 接續**：archive 卡的 job 記錄就是後續會綁到的
  那一筆，其 `worktree` 是 ship 樹且**仍在磁碟上**（post-archive 的 verify／review 卡要
  用），ship audit 兩張卡皆 passed。範本為 #654 的同名測試，改走 validator 的正式路徑。
- **`matches_candidate()` 的 ancestry 別弄壞**：archive 之後以 archive commit 為 base 用
  真的 `ScriptWorktreeCreator` provision repair clone（#651 post-archive `retry-build` 的
  回歸守衛）、做 descendant、經 bundle 回收，祖先關係在來源樹上驗得出來，ship audit 走
  得通。
- **`direct` 零回歸**：`PSC_JOB_RUNNER` 兩種值下走完全相同的路徑，七項結構性事實逐項
  相等（#634 的「以形狀判斷，不依旗標分支」；本 PR 沒有引入任何依該旗標的分支）。
- **#638 的教訓**：真正要驗的是 OS 層語意——builder 的 clone 為 `0700 cortex-builder`、
  ship 段以 `cortex-manager` 執行、pool 底下零 `setfacl`。單 UID 的開發機與 CI 三者皆無
  （同 UID 下 owner 隨時 `chmod` 回去，root 更完全不受限）⇒ **明確 `pytest.skip` 並說明
  理由**，skip 之前先斷言可測的那一半（工作區是這個行程 clone 的、獨立 object store、
  不是 linked worktree）。權限面的端到端驗證屬 runbook 的實機稽核。

**突變驗證**（兩次）：

1. 把 `validate()` 的工作區改回「builder job 記錄的那棵樹」⇒ 8 條紅 7 條，其中 chmod
   那條紅在 **`PermissionError(13, 'Permission denied')`**——本票要修的生產症狀逐字現場；
   另外三條紅在 `archive diff escaped strict OpenSpec/docs/changelog allowlist`
   （report 弄髒 candidate）與 `repo_root origin remote must match requested repo`。
2. 拿掉重用前的 pristine reset ⇒ 重用那條紅在殘留的 `leftover.txt` 還在。

既有測試的更新都是同一件事的直接後果：三個假 runner 原本寫死「archive／push／preflight
發生在哪棵樹」，改成以呼叫端實際給的 `cwd`／`-C`／`repo_root` 為準——那本來就是真指令的
行為，寫死一棵樹測到的是 fixture 自己的形狀。`test_delivery_report_cleanup_rejects_hash_drift_without_deleting`
隨被移除的函式一併刪除。

`python3 -m pytest tests/ -q`：**4017 passed, 14 skipped**（已 rebase 到含 #655／#629
四分帳號的 main）。#655 改的是 gate **執行身分**（`gate_runner.run_declared_gates()`、
`gate-ledger-spool`、`gate-worktree`），落在 build／verify 的 terminal 採信路徑上；
ship 段不讀 gate ledger（`work_bridge` 內零 `ledger` 引用），兩者不相交，rebase 後
全套與兩個相關檔案（`test_gate_execution_identity_629.py`／本檔）皆綠。

## 未做的實機驗證

`PSC_JOB_RUNNER=systemd-template` 下跑完一個含 ship phase 的 run **尚未實機執行**——本機
不是部署機。runbook 的 `%i` 稽核段已補上 ship 樹的實機稽核指令（owner ＝
`cortex-manager`、`getfacl` 具名條目數為 0）。

## 附帶發現：留給後續票

`deck/data/cards.yaml` 給兩張 ship 卡宣告的
`runtime_capabilities: ["provider:github:…", "provider:executor"]`（#369／#442）掛在
`_runtime_preflight_gate` 上，而那在 dispatch 路徑內——ship 卡不 dispatch，那兩條宣告在
生產環境**無法生效**，#442 的「先在 ship-phase 兩張卡觀測」因此觀測不到任何東西。本 PR
不處理：讓它生效需要決定「不 dispatch 的卡怎麼套 capability gate」，那是 capability 模型
的設計題，不是工作區歸屬問題。

另一個相鄰缺口（同樣不在本票範圍）：verify／review 卡的 `workflow_repo_root` 仍是
**builder 的 clone**（`manager._dispatch_workflow_card()` 的
`builder_jobs[-1]["worktree"]`），因此那條路徑在降權模式下仍會撞上同一堵牆——那是 #650
的範圍，兩票的收斂點是同一個「candidate 樹從哪來」。
