# Issue #658：build 卡被採信之後即時回收其工作區——解耦已完成，卡在「誰以什麼身分回收」

## 問題

#648 把 canonical lane 的工作區改成 per-job，一個 run 因此會累積 N 棵約 35MB 的 clone
（N ＝ build 卡數）。#649／#653（ship 段自己的 Manager-owned 樹）與 #650／#659
（verify／review 的 candidate 樹改由 Manager 自己 clone）落地之後，**一張 build 卡被
採信、`_harvest_build_candidate()` 走完之後，它的工作區已經沒有任何下游消費端**——
candidate 已在來源樹的 `refs/heads/<branch>`、bundle 已封存進 Manager-owned spool、
後續卡不再讀它。#650 驗收第 3 條「卡被採信之後即時回收該卡工作區」因此落到本票。

票上點名它不是一行：`worktree_reclaim.reclaim_worktree()` 的模組契約含**不銷毀證據**
（未提交／未追蹤內容要先複製到 `preserve_root` 封存），而三分部署下 Manager 讀不進
builder 的 `0700` clone。三個問題要一次講清楚：**誰以什麼身分回收**、**封存還剩什麼
要保**、**abandon（#613）＋ retry-card（#601）的重入**。

---

## 問題 1：誰以什麼身分回收 ⇒ **Manager，且不新增任何身分、不新增任何授權**

答案由一條**既有的**不變式決定，不是新的設計裁決：

> 抵達即時回收必須先走完 `_verify_build_candidate_transition()`，而它的第一件事是
> `_verify_exact_candidate()`——以 **Manager 身分**對**同一棵樹**跑
> `git -C <worktree> cat-file` 與 `rev-parse HEAD`。

也就是說 **「Manager 進得去那棵樹」本來就是「這張卡被採信」的必要條件**。回收要的權限
是採信路徑已經在用的那一份，**回收不需要比採信更大的授權面**。

票上四個候選逐一被否掉：

| 候選 | 為什麼不選 |
|---|---|
| (a) job wrapper 在自己退出前自刪 | 紅線（model 自證的變形）；更根本的是 job **不知道自己有沒有被採信**（採信在它退出之後由 Manager 判定），自刪必然把**未採信**路徑的殘留一起銷毀——那正是 #601 重派要用的東西 |
| (b) #629 的第三執行身分（`cortex-gate`） | 登記表給 `GATE` 對 `repo-worktree` 的是 `rX` **無 `w`**。授它 `w` 等於讓一個專門跑不受信任程式碼的帳號能改 builder **尚未 harvest** 的交付樹，與 #629 自己的論證方向相反 |
| (c) systemd `ExecStopPost=`／`RuntimeDirectory` | 時機錯：unit 停止是 **job 退出**不是**被採信**，失敗形態同 (a)；且要讓它跑得動 `rm -rf` 需要 `+` 前綴（root 執行），與「cortex 任何元件永不具 root」這條既有裁決相斥 |
| (d) 只在 `direct` 模式即時回收，降權模式交給 `cortex work gc` | #634 的反模式（依 `PSC_JOB_RUNNER` 分支）。真正決定回收成不成立的是**磁碟上的 owner**，不是旗標——以旗標分支會在「旗標說降權、磁碟其實還是 Manager-owned」與其反面各錯一次 |

改採**能力判定**：前置條件不成立就具名 skip，收不掉就 `failed` ＋ 診斷，**兩者都不擋
採信**。

### 三分／四分部署的誠實邊界（票上前提的一處修正）

票上說「preserve 那一步在降權部署下必然失敗」。實際查證：`_dirty_entries()` 讀不到時
是**記 warning 後繼續**（不是 `failed`），真正 fail-closed 的是後面的 `rmtree`——結論
（Manager 收不掉那棵樹）不變，但失敗點不在票上寫的那一步。

更關鍵的一點票上沒提：**#641 收掉 ACL 之後，降權部署下 `_verify_exact_candidate()`
就已經先 fail-closed 了**（訊息明文 `blocked on #629`）⇒ 那個部署形態下**今天根本
不存在「被採信卻沒回收的工作區」**，本函式連跑都跑不到。因此本票不必、也不得為它發明
新的執行身分。等 #629 把 candidate 驗證搬到 gate 執行身分之後，「誰讀得到那棵樹」會
跟著改變——**屆時回收身分必須與 candidate 驗證身分一起重新裁決**，這條依賴明文寫進
`_reclaim_trusted_build_workspace()` 的 docstring 與 runbook 稽核 5c。

---

## 問題 2：封存還剩什麼要保 ⇒ **契約改了**：preserve 拆成兩個具名模型

`worktree_reclaim` 的「不銷毀證據」從一種做法拆成兩個**具名**模型，呼叫端必須明講
（未知值一律 `raise`，**不靜默退回預設**）：

- `EVIDENCE_PRESERVE`（**預設，語意與 #478 逐字不變**）：所有**未採信**路徑——
  `recover-pre-candidate`、`abandon` 的回收、#601 的殘留。這些路徑的共同性質是
  **成果沒有被 harvest 過**，工作區裡的 commit 只存在於它自己的 object store。
- `EVIDENCE_HARVESTED`（新增）：呼叫端必須先證明「每一樣受治理的東西都已經有第二份
  副本，且那份副本在 Manager-owned 的樹裡」。

### 為什麼 `harvested` 底下可以放棄 preserve（逐條盤點，不是省略）

| 工作區裡的東西 | 第二份副本 |
|---|---|
| 被採信的 commit | 來源樹的 `refs/heads/<branch>`（#637 bundle ＋ spool；`_harvest_build_candidate()` 強制 branch head 恰等於 candidate） |
| bundle 本身 | Manager-owned 的 `commit-spool`（封口後仍在） |
| gate ledger | `gate-ledger-spool` → Manager 重寫的 `gate-ledger`（#628／#629） |
| exit sentinel | Manager 自己寫（#604／#628） |
| JSONL log | `log_dir`（Manager-owned），本來就不在工作區裡 |
| 宣告的 outputs | 採信當下已由 `_read_job_workflow_evidence()` 逐檔 hash 進 immutable canonical evidence |
| canonical report | #650／#659 之後發佈在 reviewer 的 candidate 樹，不在 build 卡的工作區 |

**剩下的只有「模型做了、但既沒 commit、也沒宣告為 output」的未追蹤殘渣。** 它在採信面
上的地位是零（#540 採信的是 candidate commit；#628 明講被驗方不得產生自己的驗收證據），
而把它複製進 Manager-owned 的 `evidence/` 樹實際效果是**把不受信任的內容搬進受信任的
樹**，並且把要回收的位元組原地搬個家（一張卡可能是 512 檔 × 4MB）——與本票要解的問題
是同一個換個目錄。

**#478 的現場不適用**：那次遺失的是 operator 自己 worktree 裡的真實工作，發生在**未
採信**路徑上。那條路徑仍然、也必須走 `EVIDENCE_PRESERVE`。

**放棄了什麼、保留了什麼**：放棄的是「採信路徑上的 preserve 封存」。**`archive_workspace_head()`
兩種模型下都照跑**——它是模型選錯時的安全網（commit 沒進來源樹時把它救回封存命名
空間），不是可有可無的加分項。

---

## 問題 3：abandon（#613）＋ retry-card（#601）的重入

- **retry-card／未採信路徑**：即時回收**只在採信之後跑**，失敗的 job 從來沒有被採信過
  ⇒ 它的工作區一個位元組都不動，#601 的重派前殘留仍在原地（有測試釘住，含未追蹤檔的
  內容）。#601 的範圍與處置**完全不變**。
- **retry-card 的重派**：`_manager_reset_workflow_for_retry_card()` 不動 `candidate_head`，
  重派的 base 走 `_workflow_build_handoff_base()`＝來源樹上被採信的 candidate，**不讀任何
  工作區**（#648 已釘）⇒ 即時回收不產生新的死路。
- **abandon**：`work_actions._reclaim_abandoned_build_worktrees()` 掃 run 名下每一列 job
  的 `worktree`；已被即時回收的路徑判定為 `absent`（**成功**，不是 `failed`），還活著的
  那一棵照樣收得掉。有測試走真的掛點。
- **#613 的 branch 回收**：即時回收**一個 branch 名都沒碰**，仍是 #613 的範圍。另補一條
  測試：所有 build 工作區被回收之後 `gc` 的 `protected_branches` 這條保護被抽掉了，但
  「未 merge ⇒ keep」仍頂得住，run 中途的 `gc --apply` 不會刪掉交付 branch。

---

## 落地

`paulsha_cortex/coordinator/worktree_reclaim.py`

- 新增 `EVIDENCE_PRESERVE`／`EVIDENCE_HARVESTED` 與 `reclaim_worktree(..., evidence_model=)`
  （`reclaim_worktrees`／`reclaim_recorded_or_derived` 一併透傳）；未知值 `raise`。
- `WorktreeReclaim` 增 `evidence_model` 欄並帶進 `to_dict()`——operator 看得到某一次回收
  **為什麼**沒有 preserve 封存。
- 模組 docstring 改寫「不銷毀證據」那一條為兩個模型 ＋ 完整論證。

`paulsha_cortex/coordinator/manager.py`

- 新增 `_trusted_build_workspace_target()`：六條前置條件，前四條防「刪到不該刪的東西」、
  後兩條防「刪到還沒有第二份副本的東西」。安全閘的核心是**目錄名必須恰好是這個 job_id
  經 `job_workspace.job_segment()` 導出的片段**（#645 的單一推導點）——#549 的資料語意
  地雷（`job.worktree` 等於 run 的 `workspace_root`）在這裡結構性被擋掉。刻意**不**拿
  `paths.worktree_root_for()` 當判準：那是 config 解析出來的位置，會與磁碟上真正
  provision 到哪裡漂開（#634「以形狀判斷，不依環境推導」）。另外三條：標記檔存在
  （#646「認不得就不刪」）、標記檔的 `branch`／`source_repo` 對得上、以及**當場對來源樹
  複驗** `commit_present()` 與 `source_branch_head() == candidate`。
- 新增 `_reclaim_trusted_build_workspace()`：**永不 raise**，三種結果各寫一行結構化 log
  （`workflow-build-workspace-reclaim-{reclaimed,skipped,failed}`）。
- 呼叫點在 `apply_workflow_action("advance")` 的 build 分支、**`_manager_update_workflow_run()`
  之後**。位置是刻意的：掛在 `_harvest_build_candidate()` 旁邊的話，中間任何一條 raise
  都會留下「工作區已刪、卡仍 pending」，下一個 tick 的 `_verify_exact_candidate()` 會以
  `git -C <已刪的路徑>` 收場——一條**由清理製造出來的死路**。落盤之後再收，重入撞到的是
  既有的 `workflow card evidence replay rejected`（已採信的卡不再讀工作區）。

`docs/superpowers/runbooks/trust-root-phase2b-setup.md`：新增稽核 5c（回收沒有偷偷把
#641 收掉的 ACL 加回來；運轉期以 journal 事件與 pool 佔用對照）。

## 紅線遵守

- **沒有**加回 Manager 對 job 工作樹的 ACL（#641／#644）——回收用的是採信路徑既有的
  那一份權限，稽核 5b 的「零 `setfacl`」逐字仍然成立。
- **沒有**讓 job 自己決定何時回收自己（回收由 Manager 在採信之後觸發）。
- **沒有**寫會把不認得的目錄直接刪掉的清理器（#646）——六條前置條件全部 fail-closed。
- `direct` 模式零回歸：本次沒有引入任何依 `PSC_JOB_RUNNER` 的分支，也沒碰
  `job_runner`／`launcher`／`permgen`／`trust_root/`。

## 測試

新增 `tests/test_immediate_worktree_reclaim_658.py`（15 條 ＋ 1 條 skip），全部跑正式
路徑：真 git repo、真 `ScriptWorktreeCreator` 的 per-job clone、真
`dispatch_workflow_card()`、真 bundle ＋ append-only spool 交接、真
`terminalize_workflow_job()`、真 `apply_workflow_action(action="advance")`、真
`_manager_reset_workflow_for_retry_card()`、真 `gc.scan()`。

**突變驗證**：停掉即時回收呼叫 ⇒ **6 條轉紅**；拿掉「目錄名 ＝ job_segment」與「來源樹
複驗」兩道安全閘 ⇒ **3 條轉紅**。

**#638／#657 的教訓**：跨帳號擁有權語意（`0700 cortex-builder` 的樹由 `cortex-manager`
回收）需要 root 建置 ＋ 以另一個 UID 執行回收，**單一 pytest 進程結構性做不到** ⇒ 明確
`pytest.skip` 並在 reason 印出本機實況。可測的那一半（讀不進去時得到具名拒絕理由、
不做注定失敗的 `rmtree`）另有一條 `chmod 000` 的測試，並在 docstring 明講它證得了什麼、
證不了什麼。

`python3 -m pytest tests/ -q`：**4063 passed, 19 skipped**。
