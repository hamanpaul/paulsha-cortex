# canonical lane 的工作區改為 per-job（#648）

## 問題

canonical（workflow）lane 的工作區是 **per-run** 的：build 卡 provision 之後，同一個
run 的後續卡直接沿用 `builder_jobs[-1]["worktree"]`——**一個工作區對多個 `job_id`**。

trust-root Phase 2b 的 template unit 是 per-job 定址的（`ReadWritePaths=<pool>/%i`，
`%i` ＝ systemd instance ＝ job id）。一個工作區不可能同時等於多個 job id，所以在
`PSC_JOB_RUNNER=systemd-template` 下 canonical lane 的卡必然拿到指向不存在路徑的
`ReadWritePaths` → `Failed to set up mount namespacing` → `226/NAMESPACE` → 起不來。
#645／#646 只把命名收斂成單一推導點，**沒有解決 per-run 對 per-job 的結構落差**，
並在程式碼裡明文把 canonical lane 排除在不變式之外。本次把那個排除拿掉。

## 改法

### 1. 工作區 per-job，目錄名沿用唯一推導點

`manager._dispatch_workflow_card()` 的 build phase 一律 provision **自己的** clone，
目錄名 ＝ `job_workspace.job_segment(job_id)`——與 `job_runner.template_instance_id()`
是同一個函式的同一個輸出（不是「兩邊各自算、剛好相等」，那正是 #645 的形狀）。
slice lane 的推導點一個位元組都沒動。

### 2. job_id 必須在 provision 之前定案 → `registry.reserve_job_id()`

目錄名由 job_id 導出，而 `create_job()` 的 `workflow_input_snapshot` /
`workflow_output_baseline` 都是從**工作區裡的檔案**算出來的——順序只能是
「先配 id → 建工作區 → 建 job」。因此新增 `JobRegistry.reserve_job_id(task)`：
配發即消耗（`_seq` 前進並落盤），呼叫端把拿到的 id 原樣交回
`create_job(job_id=…)`，那裡驗證它確實屬於同一個 `task`、未被使用、且已配發。
**不是「預測下一個 id」**——預測會在任何一次插入之後漂掉。
`create_job()` 與 `reserve_job_id()` 共用同一個私有配發器 `_allocate_job_id()`，
`f"{task}-{seq}"` 這條公式全 repo 仍然只有一份。

### 3. 卡與卡的交接顯式化：bundle ＋ append-only spool（沿用 #637）

per-run 工作區隱含「前一張卡的產出留在磁碟上給下一張用」。per-job 之後那條交接
改走 #637 已落地的通道：

```
builder 在自己的 clone 產 bundle → Manager-owned append-only spool
  → Manager 從那個檔案 fetch 進來源樹的 refs/heads/<branch>
    → 下一張卡從**來源樹** clone
```

`_harvest_build_candidate()` 已經強制「回收後來源樹的 branch head 恰等於被採信的
candidate，對不上即 fail-closed」，因此

    來源樹的 refs/heads/<branch> == run.candidate_head

在每一張 build 卡被採信之後成立。新增的 `manager._workflow_build_handoff_base()`
就以 `run.candidate_head` 作為後續卡的 clone base——**完全不讀前一張卡的工作區**。
測試把「前一張卡的工作區整個刪掉，後續卡仍拿得到正確 base」釘成不變式。

### 4. base 推導（含中段卡重派）

| 情形 | base |
|---|---|
| 首張 build 卡 | `frozen_readiness["base_sha"]`（#208／#211），無凍結集則不傳 |
| 後續／中段 build 卡 | `run.candidate_head`（最後一張**被採信**的卡的 candidate） |
| `candidate_head` 尚未錨定（首張卡的 terminal 壞掉正在重派） | 同首張卡 |

`retry-card`（#545）刻意不動 `candidate_head`，因此重派的中段卡拿到的 base 仍是
最後一張被採信的 candidate——不是 run 的原始 base（會丟掉前面幾張卡的成果），也不是
那次失敗嘗試留在磁碟上的東西（那是另一個 job_id、另一個目錄）。

推不出合法 SHA 時 **raise**，不得退回 creator 的預設 base（那是 `main`，
`branch -f` 會把整個 run 已採信的 commit 從 branch 上抹掉）。base 與來源樹實況對不上
時由 `ScriptWorktreeCreator.create()` 既有的兩道守衛擋下：`rev-parse --verify <base>`
找不到 ⇒ 交接沒走完；`merge-base --is-ancestor <branch> <base>` ⇒ branch 上有 base
以外的 commit（#613 的形狀）。兩條都 fail-closed，訊息逐字沿用。

## 成本

每張 build 卡一次 clone（實測 0.5 秒／35MB）。`feature-oneshot` 的 build phase 是
三張卡（`worktree-isolation` / `tdd-red` / `subagent-build`）⇒ 約 **1.5 秒／105MB**，
相對於一張卡動輒數分鐘的模型執行時間可忽略。

**刻意不做 `--reference`／`--shared` 之類的優化**：那會把 object store 接回共用，而
#623 已判定「共用 object store」與「三分隔離」**互斥**（builder 要 commit 就必須能寫
object store）。要優化只能走不共用 object store 的路（例如 shallow／partial clone），
不在本票範圍。

## `gc` / `worktree_reclaim` 受什麼影響

- **`gc.py`：不必改**。判準是**形狀**（`job_workspace` 標記檔／`.git` 檔）與 branch
  的 merge 狀態，不看目錄名、也沒有「一個 run 一個工作區」的假設。同一條 branch 上
  掛著 N 個工作區時 N 個都掃得到；只要還有任何一個被 keep，那條 branch 就受保護
  （`protected_branches`），不會在 job 還活著時被刪掉。新增測試釘住。
- **`worktree_reclaim.py`：不必改，而且變得更安全**。它收的是呼叫端給的**路徑**。
  per-job 之後每一列 job 記錄的 `worktree` 都是自己那一個，回收一張卡不會波及同 run
  的其他卡——per-run 時代那是同一條路徑，回收任一張卡等於把兄弟卡的樹一起刪掉。
- **#601（retry-card 不回收 provision 卡的殘留 worktree）：症狀消失，票不關**。
  #601 的生產現場是重派撞 `worktree target already exists`；per-job 命名之後兩次嘗試
  的目錄名不同，那個撞名在 canonical lane 上**結構性消失**。但殘留目錄仍留在磁碟上，
  「回收」那一半仍是 #601 的範圍。
- **#613（abandon 不回收 build branch）：完全無關**。本次一個 branch 名都沒改。
- **新增的成本面**：一個 run 現在會累積 N 個工作區（以前 1 個）。canonical lane 的
  abandon 目前本來就不回收工作區（那是 #595／#613／#558 那一族），既有的掃除路徑
  `cortex work gc` 不看名字、N 個一起收得掉。**卡被採信之後即時回收**現在是安全的
  （成果已在來源樹、bundle 已封存），列為後續票。

## `direct` 模式零回歸

branch 名、來源樹的 `refs/heads/<branch>`、工作區自己 checked-out 的 branch、標記檔的
`branch` 欄、`spool_key_for_job()`（由 `log_path` 推導）、`dispatch_head`（仍是 **run
層級**的 base，persona scope diff 的比較基準）全部逐字不變，各有測試釘住。
`direct` 與 `systemd-template` 走**完全相同**的 provisioning 路徑（本次沒有引入任何
依 `PSC_JOB_RUNNER` 的分支）。

## 不在本票範圍（已切成後續票）

- **ship phase 的 manager 卡**（`openspec-archive` / `policy-commit`）仍沿用
  `builder_jobs[-1]["worktree"]`。它們是降權派工的對象（manager persona 既非
  `read_only` 也非 `review_only`），因此 `%i` 不變式同樣落在它們身上——但它們的成果
  目前**沒有 harvest 通道**（`_harvest_build_candidate()` 只在 `current_phase ==
  "build"` 時跑），先改 per-job 會讓第二張 ship 卡看不到第一張的 commit。要先補
  「ship phase 成果回收」才動得了。
- **verify / review 卡的 candidate 樹**仍讀前一張 build 卡的工作區。這兩種卡不是降權
  對象（reviewer 走 `as_review_only()`），實際工作樹是 reviewer sandbox，因此 `%i`
  不變式不落在它們身上；但它們確實還依賴前一張卡的磁碟殘留，這是「即時回收」的前置。

## 測試

新增 `tests/test_canonical_per_job_workspace_648.py`（10 條，全部跑**正式** dispatch
路徑：真的 `ScriptWorktreeCreator`、真的 git repo、真的 bundle ＋ spool 交接）：

- **不變式（本票的全部價值）**：canonical lane **每一張** build 卡的工作區目錄名 ==
  `job_runner.template_instance_id(launch(slice_id=…) 收到的那個 id)`。兩側都是真實
  推導函式，**不對常數斷言**。
- **突變守衛**：#648 之前的 per-run 目錄名（`job_segment("<issue>-<work_id>")`）與
  #645 之前的 branch slug 都不得再出現在 pool 裡。
- **交接不依賴磁碟殘留**：把前一張卡的工作區 `rmtree` 掉，後續卡仍 clone 出帶著它
  成果的樹（`HEAD == candidate`、檔案在、`refs/cortex/base` 也錨在 candidate 上）。
- **fail-closed**：candidate 沒回到來源樹時拒絕 provision，不退回 run 的原始 base。
- **中段卡重派**：走真的 `_manager_reset_workflow_for_retry_card()`，新目錄、新
  instance 名、base 仍是最後一張被採信的 candidate；失敗那次的殘留一個位元組沒動。
- **`dispatch_head` 仍是 run 層級**：per-job 只改工作區，不改記帳基準。
- **`direct` 模式零回歸** ＋ **`reserve_job_id()` 的三條守衛**。
- **gc／reclaim**：同一條 branch 上多個工作區都掃得到、branch 受保護、回收一張卡不
  波及兄弟卡。
- **完整 `prepare_systemd_template()`**：需要 OS 層前置物（`cortex-builder` 帳號／
  group、兩份模板 unit、shim、spec spool）。單 UID 的開發機與 CI 沒有，且任何單 UID
  的模擬都測不出 mount namespace 的語意 ⇒ 比照 **#638 的教訓明確 `pytest.skip` 並逐項
  列出缺哪一個**，不靜默通過。

`python3 -m pytest tests/ -q`：**3892 passed, 6 skipped**。

## bootstrap

`cortex work intake` 走的正是 canonical lane。本票之前，canonical lane 在降權模式下
build phase 起不來，cortex 因此無法在降權模式下自己修自己。本票讓 build phase 可用，
自我託管的第一段因此打通（ship phase 待後續票）。
