# Issue #649：ship phase 的成果回收缺席——`openspec-archive` 的 commit 沒有進來源樹

## 查證（本票的第一步，結論改變了範圍）

### 1. ship 卡**不是**降權派工的對象——票上的前提不成立

`openspec-archive`／`policy-commit` 兩張 ship 卡的 `persona_binding` 是 `manager`
（`deck/data/cards.yaml`；`workflow.py` 的 manifest 也硬性要求 `"ship": "manager"`），
而 `manager._dispatch_workflow_card()` 的第一道判準是

```python
if step is None or run.current_phase not in {"plan", "build", "verify", "review"}:
    return None
```

**ship phase 永遠回 None**。這兩張卡不經任何 launcher、不 spawn 任何 job——它們由
Manager 自己在 `work_bridge.py` 內以 deterministic 身分執行
（`_commit_archive_and_require_reverification()` 直接 `git commit`，
`_record_manager_ship_job()` 事後補一筆 `executor=cortex-manager` /
`model_id=deterministic` 的稽核用 job 記錄）。

因此：**沒有 template unit、沒有 `ReadWritePaths=<pool>/%i`、沒有 `226/NAMESPACE`。**
`launcher._downgraded_mode()` 落不落在 manager persona 上是個假問題——那條路徑根本
不會被走到。#649 票上「#648 的症狀在 ship phase 原封不動」這句是**錯的**，
「ship 卡的工作區改 per-job」也因此失去它原本的動機（見「怎麼切的」）。

> 附帶發現（不在本票範圍，另記）：`cards.yaml` 給這兩張卡宣告的
> `runtime_capabilities: ["provider:github:…", "provider:executor"]`（#369／#442）
> 掛在 `_runtime_preflight_gate` 上，而那也在 dispatch 路徑內——ship 卡不 dispatch，
> 那兩條宣告在生產環境同樣無法生效。

### 2. `matches_candidate()` 的 ancestry 檢查：**目前是壞的**（fail-closed）

`manager._validated_ship_steps()` 內的 `matches_candidate()` 容許
`openspec-archive` 的 `subject_head` 是 final candidate 的**祖先**，並在
`run.workspace_root`（來源樹）上跑 `git merge-base --is-ancestor`。

- **什麼時候會走到**：只有 post-archive repair——archive commit 之後 review 再度提出
  阻擋性 findings、operator 下 `retry-build`、builder 在 archive commit 之上做出
  descendant candidate。正常流程下 `subject_head == candidate` 精確相等，走第一個
  分支，ancestry 那段碰不到。
- **走到之後成不成立**：**不成立**。archive commit 是 Manager 在**工作區**裡做的，
  而 #623 把工作區從 `git worktree`（與來源樹共用 object store）換成 per-job 完整
  clone 之後，來源樹的 object store 裡**沒有那個 commit**。
  `git merge-base --is-ancestor <來源樹沒有的 sha> <candidate>` 回 **128**
  （`fatal: Not a valid commit name`），不是 1；`matches_candidate()` 因此回 False，
  該卡被濾掉，`_validated_ship_steps()` 以
  `workflow ship card audit missing or ambiguous: openspec-archive` 中止。

  也就是說：這條檢查在 #623 之後就是一條**只會 fail-closed 的死路**。它是安全的
  （不會誤放行），但 post-archive repair 這條路走不完。
- **既有測試為什麼是綠的**：
  `tests/test_workflow_production_wiring.py::test_ship_audit_accepts_manager_archive_ancestor_after_retry_build`
  的 fixture 把 archive commit 與 repair commit **都直接做在 `run.workspace_root`
  裡**——那正是 #623 之前共用 object store 的形狀。fixture 幫產品把前提補上了，
  於是測試看不到 production 的斷點。

### 3. #651 讓同一個缺口多長出一個症狀

#651 之後 build 卡改成 per-job clone，中段／後續卡的 base 是
`_workflow_build_handoff_base()` 給的 `run.candidate_head`。post-archive 的
`retry-build` 因此會拿 **archive commit** 當 base 去 provision，而
`ScriptWorktreeCreator.create()` 的第一道守衛是在**來源樹**上
`rev-parse --verify <base>^{commit}`——找不到 ⇒ `git worktree base invalid`，
重派連工作區都建不起來。（本 PR 的測試把 harvest 拿掉之後，重現的正是這句話。）

## 回收模型：選路 (a)（bundle ＋ append-only spool），producer 換成 Manager

票上兩條路擇一。選 (a)，推導如下：

- **不能選 (b)（「ship 卡的 commit 不進 candidate 鏈」）**：archive commit **就是**
  下一輪的 candidate——`_manager_reset_workflow_after_archive()` 把
  `candidate_head` 推到它、verify／review 對著它重跑、`_builder_binding()` 以
  `subject_head == candidate` 選工作區、`policy-commit` 精確綁 final candidate、
  PR 推的也是它。把它排除在鏈外等於重寫整條 post-archive 語意，而
  `matches_candidate()` 的 ancestry 檢查（＋ `governed-delivery-closure` spec 對它的
  MUST）也得跟著拆——那不是縮小範圍，是換一套設計。
- **選 (a) 但通道的 producer 是 Manager 自己**：#637 的 bundle ＋ spool 之所以存在，
  是為了解兩個問題——builder 不能 push 進 Manager 的樹（D2 單向性），以及 Manager
  走不進 builder 的樹（0700 ＋ `safe.directory` 不吃路徑 glob）。archive commit 由
  Manager 親手做出來，沒有 wrapper script 可以掛
  `job_workspace.build_bundle_command()` 那段 shell，但**consumer 那一半必須是同
  一個**：`harvest_branch()` 是全 repo 唯一的「commit 進來源樹」實作，繞過它另寫一次
  `git -C <來源樹> fetch <那棵工作區>` 會同時複製一份 fail-closed 分類，並且把
  `job_workspace` 模組 docstring 明文否決的形狀寫回程式碼（那也正是下一段
  「ship 工作區搬進 Manager-owned clone」之後必炸的形狀）。

  因此只補 producer 那一半：新增 `job_workspace.publish_commit_bundle()`
  （in-process 版的 `build_bundle_command()`），spool 那一格沿用
  `prepare_commit_spool()` / `seal_commit_spool()`，key ＝ 這張 ship 卡的 job_id。
- **票上「launcher 本來就已經幫 manager persona 建了 spool」這句也不成立**：ship 卡
  不 launch，`prepare_commit_spool()` 從未為它跑過；`spool_key_for_job()` 由
  `log_path` 推導，而 ship 卡的 job 記錄沒有 `log_path`。本 PR 因此在 Manager 側
  顯式配 id、顯式建那一格。

### 落地

`work_bridge._commit_archive_and_require_reverification()` 在
`git commit` 之後、`_record_manager_ship_job()` 與
`_manager_reset_workflow_after_archive()` **之前**插入：

```
registry.reserve_job_id(_manager_ship_job_task(...))       # 先配 id
  → job_workspace.prepare_commit_spool(spool_key=<那個 id>)
  → job_workspace.publish_commit_bundle(工作區 → 那一格)
  → job_workspace.harvest_branch(bundle → 來源樹的 refs/heads/<branch>)
  → 回收後的 branch head 必須恰等於剛做的 commit，對不上即 fail-closed
  → seal_commit_spool()
```

順序不能反過來：`candidate_head` 一旦推進，整條鏈（ship audit 的 ancestry、
post-archive retry-build 的 clone base、下一張卡的 handoff base）就開始假設來源樹有
那個 commit；回收失敗必須在推進**之前**擋下，而不是讓錯誤在很遠的地方以看不懂的訊息
出現。

另外兩道守衛：

- **commit 必須在記錄的 branch 上**（`source_branch_head(worktree, branch) == new_head`）
  ——回收的 refspec 是 `refs/heads/<branch>`，detached HEAD 或第三方動過 ref 時
  bundle 帶的就是別的東西。與 `_harvest_build_candidate()` 的 head mismatch 同一條
  判準。
- **bundle 的排除點取決於來源樹有沒有那個 commit**（`commit_present()`）：正常情形是
  `^<archive 之前的 candidate>`，只搬一個 commit；來源樹沒有它時（升級前既存的 run、
  或從未走過 build harvest 的路徑）改為不排除、帶完整歷史——寧可多搬一點，也不要因為
  缺 prerequisite 讓一次**合法**的回收失敗。

`matches_candidate()` **一個位元組都沒改**：本 PR 修的是它的前提。archive commit
回到來源樹之後，那條 ancestry 檢查就回到它原本設計成立的樣子。

## 怎麼切的（範圍縮小，理由是查證結果而不是做不完）

**本 PR 只做成果回收**。票上第 3 點「ship 卡的工作區改 per-job」不做，理由是查證
推翻了它的動機、並換上一個**更大也更真**的需求：

- **原動機消失**：ship 卡不 dispatch、不 launch，`%i` 不變式落不到它們身上
  （見查證 1）。照票面把 `reserve_job_id() → creator.create()` 套上去，得到的是
  一個沒有人會用 `%i` 定址的目錄——為了一個不存在的約束改動整條 ship 路徑。
- **真正的阻擋物是別的**：ship 段全程在 `_builder_binding()` 交回來的
  **builder 的 clone** 裡動手（`git diff`／`add`／`commit`／`rev-parse`／`push`、
  `Path(worktree).resolve(strict=True)`、`_ship_action(repo_root=…)` 連測試都在那裡
  跑）。Phase 2b 三分下那棵樹是 `cortex-builder` 0700，而 #641 已經把登記表裡
  Manager 對 job 工作樹殘留的讀取授權**全部收掉**（runbook 第 2 步的稽核 5b 要求
  `/var/lib/cortex/worktree/` 底下零 setfacl）。也就是說 ship phase 在降權模式下會
  在**第一個 `git -C`** 就 `Permission denied`——不是 `226/NAMESPACE`。
- 修法是把 ship 段的 git 工作搬進一棵 **Manager-owned** 的樹（此時 per-job 目錄名才
  有意義），連帶要處理 `_builder_binding()` 的來源、`archive-applied-needs-commit`
  這條重入路徑、preflight／push／`_ship_action` 的 repo_root。那是獨立一票的體量，
  且**必須**建立在本 PR 的回收通道之上（沒有回收，換了樹之後 candidate 更是回不來）。
  已開後續票追。

本 PR 因此是「能獨立驗證的第一段」：它自己就修好三個現行缺陷（ancestry 死路、
#651 的 post-archive retry-build 回歸、成果只存在於某棵工作區的磁碟上），
且不依賴後續票。

## 測試

新增 `tests/test_ship_phase_harvest_649.py`（9 條，全部跑正式路徑：真 git repo、真
per-job clone、真 `work_bridge._commit_archive_and_require_reverification()`、真
`seams.ScriptWorktreeCreator`）：

- **回收本身**：archive commit 進來源樹，且 `refs/heads/<branch>` == 新 candidate。
- **交接不依賴磁碟殘留**：把做 commit 的那棵工作區整個 `rmtree` 掉，commit 與檔案內容
  仍在來源樹裡讀得出來（#651 對 build 卡釘的是同一個形狀）。
- **fail-closed 且不先推進**：commit 落在 detached HEAD 上時拒絕回收，且
  `candidate_head`／`current_phase`／來源樹 branch 全部**原封不動**。
- **`matches_candidate()` 的 ancestry（本票的查證題）**：正向——回收之後，以 archive
  commit 為 base 用**真的** `ScriptWorktreeCreator` provision 出 repair clone、做出
  descendant、harvest 回來，ship audit 兩張卡都 passed（這條同時是 #651
  post-archive retry-build 的回歸守衛）；反向——archive commit 沒回收時 ship audit
  fail-closed（＝本 PR 之前的實況）。
- **`openspec-archive` → `policy-commit` 的接續**：archive 卡的 job 記錄（
  `subject_head`／`branch`／`persona`／`executor`）就是 `_builder_binding()` 之後會
  選到的那一筆；`policy-commit` 不產生 commit，來源樹 branch 一個位元組沒動。
- **spool 定址**：spool key ＝ `reserve_job_id()` 配發的那個 job_id，task 由
  `_manager_ship_job_task()` 單一推導（兩處字面值不再各寫一次）。
- **`direct` 零回歸**：`PSC_JOB_RUNNER` 兩種值下走完全相同的路徑，結構性結果逐項
  相等（#634 的「以形狀判斷，不依旗標分支」）。
- **#638 的教訓**：spool 封口對 producer 的**強制力**需要 producer 與 consumer 是
  不同 UID ＋ per-account POSIX ACL，單 UID 的開發機與 CI 都沒有，任何單 UID 模擬都
  與真部署無關 ⇒ **明確 `pytest.skip` 並說明**，skip 之前先斷言可測的那一半（封口真的
  發生）。

**突變驗證**：把 harvest 那一行停掉之後，上述 9 條有 8 條轉紅（唯一仍綠的是那條反向
的 fail-closed 測試，符合預期），其中 ancestry 那條紅在
`git worktree base invalid: fatal: Needed a single revision`——正是 #651 的
post-archive retry-build 回歸的逐字現場。

既有測試的更新都是同一件事的直接後果：三個 fixture 把「工作區 ＝ 來源樹」改成
#623 之後的真實形狀（來源樹持有 delivery branch 但不 checkout，工作區是另一棵
checkout 在該 branch 上的 clone）。共用 helper `tests/git_fixtures.py:make_job_clone()`。
沒有這一步，`git fetch` 會撞上「拒絕寫入正被 checkout 的 branch」——那是 fixture 的
問題不是產品的。

`python3 -m pytest tests/ -q`：**3900 passed, 7 skipped**。

## 未做的實機驗證

`PSC_JOB_RUNNER=systemd-template` 下跑完一個含 ship phase 的 run **尚未實機執行**
——本機不是部署機，且依上面的查證，ship phase 在降權模式下還會卡在
「Manager 讀不進 builder 的 clone」，那是後續票的範圍。本 PR 的不變式已由上述測試在
真 git repo 上端到端覆蓋。
