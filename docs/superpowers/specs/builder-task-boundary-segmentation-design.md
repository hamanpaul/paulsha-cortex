---
status: draft
work_item: builder-task-boundary-segmentation
---

# builder-task-boundary-segmentation Design

## Decisions

### D1 分段執行模型：tick loop 對同一 slice 逐 Task 反覆呼叫新的 redispatch 原語，不擴充 `Dispatcher.dispatch()`

per-Task fan-out 落在 `paulsha_cortex/coordinator/manager_daemon.py` 的 tick 迴圈
（`build_periodic_tick_runner`，862-1027 行）：由呼叫端（tick runner）在偵測到某
builder job 已終局（`exited`/`failed`/新增的 `context-exhausted`，見 D4）且該 slice
的 progress ledger（見 D5）仍有未完成 Task 時，對同一 slice 再呼叫一次派工，prompt
只含下一個 Task（見 D3）。

**不是**在 `Dispatcher` 內新增一個會自行跑迴圈的 `TaskLoop`（迴圈的驅動節奏必須跟
manager 既有 tick cadence 對齊，才能吃到既有的 backoff／held／in-flight 限流，見
`manager_daemon.py:210-224` 的 `_tick_backoff_seconds`／`_safe_tick_error_summary`），
但**也不是**直接重用 `Dispatcher.dispatch()`（108-138 行）本身：`dispatch()` 每次呼叫
都會走 `self._worktree_creator.create(branch, base_sha=...)`，而
`GitWorktreeCreator.create()`（`paulsha_cortex/coordinator/seams.py:70-76`）在目標
worktree 目錄已存在時 raise `ValueError("worktree target already exists")`——第一個
Task 派工後 worktree 已建好，第二個 Task 若照抄 `dispatch()` 的路徑會直接炸掉。

因此 D1 落地為 `Dispatcher` 新增一個**同 worktree 續派**的方法（暫名
`Dispatcher.redispatch()`），與 `dispatch()` 共用 `_pane_sender.send()` 與
`self._registry.create_job()` 兩步，但跳過 `worktree_creator.create()`：

1. 前置：呼叫端傳入前一個 job 的 `worktree`／`branch`（從 `JobRegistry.get_job()`
   讀回，不重新建立）。
2. `_pane_sender.send(pane_id, next_task_command)` 送下一個 Task 的 prompt。
3. `runner(["rev-parse", branch])` 重新取「當下」branch head 作新的
   `dispatch_head` baseline——**不可**沿用第一個 Task 的舊 baseline，否則
   `poll_done`（`dispatcher.py:145-160`）拿舊 baseline 比對，會把第一個 Task
   的 commit 誤判成第二個 Task 已完成。
4. `self._registry.create_job(task=task, persona="builder", branch=branch,
   worktree=worktree, dispatch_head=<新 baseline>, ...)` 開新 job row。
   `registry.py:730-734` 的重入防護（`persona == "builder"` 且已有
   `ACTIVE_JOB_STATUSES` job 則 raise）在此天然成立為安全網：只有在前一個
   Task 的 job 已終局（不在 `dispatched`/`running`）時才可能呼叫到這裡，
   不需要額外加鎖。

理由：把「decide whether/when to advance to next Task」的狀態機放在 tick
runner（本來就是 manager 對 slice 生命週期做決策的地方），派工原語本身維持
「一次呼叫＝一個 job row」的既有語意不變，只是新增一個不重建 worktree 的變體，
向後相容——未使用分段的既有 slice 完全不觸發 `redispatch()`。

### D2 Task 邊界供 dispatch 消費：新函式 `list_plan_tasks()`，不重用 `_collect_task_items()`

`paulsha_cortex/coordinator/planning.py:317-349` 的 `_collect_task_items(body)`
**不能**直接重用：它鎖定「Tasks」heading 層級後，逐一遇到下一個 `## Task N`
heading 時，heading 判斷分支（`if title in {"task","tasks"} or
title.startswith("task ")`）優先於「離開 task 區段」分支被命中，於是每個
`## Task N` heading 都被視為同一個「task 區段」的延續，函式回傳的是**攤平後
的單一 tuple**（所有 Task 底下的清單項目混在一起），無法回推「這個清單項目
屬於哪一個 Task N」。它現在唯一的用途是 plan review 完整性檢查（判斷 Tasks
區段是否為空），對「哪些內容屬於 Task 2」這種分段派工需要的邊界資訊完全不
保留。

因此新增 `list_plan_tasks(plan_path: str | Path) -> tuple[TaskUnit, ...]`（新
函式，放 `planning.py`，與 `_collect_task_items` 並存、不改動後者既有行為）：

```python
@dataclass(frozen=True)
class TaskUnit:
    index: int          # 1-based，對應 "## Task N" 的 N
    heading: str         # 原始 heading 文字（去除 "## " 前綴）
    body: str            # 該 heading 下、至下一個同層 heading 前的完整文字
    items: tuple[str, ...]  # body 內清單項目（未來供 checkbox 完成度判斷，見 D5）
```

解析規則沿用 `_collect_task_items` 已驗證過的 heading/fence 追蹤手法（同一顆
`_HEADING_RE`／`_LIST_ITEM_RE`／fence-skip 邏輯），差異只在於：遇到新的
`## Task N` heading 時**結束前一個 TaskUnit、開新的一個**，而不是視為延續。
`index` 直接取 heading 文字內的數字（`## Task 3` → `index=3`），非該 heading
在檔案內第幾次出現——避免 plan 作者手誤跳號或重排時 index 對不上人類可讀的
Task 編號。

### D3 prompt 模板改動：`build_dispatch_prompt()` 新增 optional `task_slice` 參數，未傳維持現行整份 plan 行為

`paulsha_cortex/coordinator/contract_command.py:9-28` 的 `build_dispatch_prompt()`
簽名擴充：

```python
def build_dispatch_prompt(
    role: str,
    *,
    task: str,
    plan_path: str,
    catalog: Mapping[str, PersonaContract] | None = None,
    task_slice: "TaskUnit | None" = None,   # 新增，D2 的 TaskUnit
) -> str:
```

`task_slice is None`（預設）時函式行為與現行**逐位元不變**——第 27 行的
`"請於本 worktree 內讀取上述 plan 並依 persona 契約邊界執行。"` 原樣保留，
現有呼叫端（未宣告分段的 slice）零改動。

`task_slice` 非 None 時，prompt 尾段換成只嵌該 Task 的內容＋反漫遊紀律語句＋
commit 斷點語句，取代整份 plan 引用：

```
[TASK: <task_slice.heading>]
<task_slice.body>

本 plan 已決策完備，請直接依上述 Task 內容動手實作，
不得重新探索已由 plan 決策的範圍、不得全庫漫遊。
本段結束前 git status 必須乾淨、必須完成至少一個 commit。
```

文案依據 issue #276 原文「已驗證有效的對策」一段實測有效的兩個變因：分段
（fresh context per Task）與反漫遊紀律（prompt 明講 plan 決策完備、rg 只准
定位）——本設計把第二個變因（機率性改善）也寫進模板，與第一個變因（結構性
保證）疊加，而非只靠分段本身。

`plan_path` 仍然保留（即使傳了 `task_slice`）：agent 仍可讀完整 plan 取得
上下文（例如跨 Task 的既有決策），只是明確指示「本段只做這個 Task」。

### D4 completion 分類擴充：新增 `context-exhausted`，貫穿 dispatcher 與 manager 恢復邏輯

`paulsha_cortex/coordinator/completion.py:60-69` 現況：

```python
def classify_completion(*, exit_code: int, last_jsonl_line: str | None) -> str:
    """exit code + 末筆 JSONL → 'exited'/'failed'。JSONL 不可解則 fallback exit code。"""
    if last_jsonl_line:
        try:
            obj = json.loads(last_jsonl_line)
            if isinstance(obj, dict) and obj.get("ok") is False:
                return "failed"
        except (json.JSONDecodeError, TypeError):
            pass  # fallback 到 exit code
    return "exited" if exit_code == 0 else "failed"
```

只有兩態，且判定只看**末筆** JSONL 的 `ok` 欄位——issue #276 引用的實測錯誤字串
`Error running remote compact task: Codex ran out of room in the model's
context window.` 出現在 `turn.failed` 事件內，若它不是最後一行（後面還有
executor 自己吐的收尾訊息），現行邏輯會直接 fallback 到 exit code，把
context-exhausted 併入一般 `failed`，無法區分。

擴充後三態：`classify_completion()` 除了原本的末筆 `ok` 檢查，新增一個
**全檔掃描**（非只看末筆）的字串比對：jsonl 內任一行含
`"ran out of room in the model's context window"`（大小寫不敏感、允許前後
文字），即回傳 `"context-exhausted"`，優先權高於 `exited`/`failed` 的判定
（即使 exit code 為 0，只要偵測到該字串就標 `context-exhausted`——因為 issue
#276 的第一個實測案例正是「exit 0 但未 commit」，context 耗盡發生在收尾前）。
簽名不變（仍是 `exit_code`＋`last_jsonl_line`），但為了掃描整檔字串，呼叫端
需要把 jsonl 全文（或至少全部行）傳入，而不是只傳末筆——這是本 D4 對呼叫端
（`dispatcher.py` 的 `_finalize_headless`）的必要連動改動，需在後續 code 票
新增一個 `full_jsonl_text` 或等價參數，向後相容做法是新增 optional kwarg，
未傳時退回只掃 `last_jsonl_line`（維持現行測試不受影響）。

貫穿路徑：`classify_completion()` 的回傳值目前經由 `dispatcher.py` 的
`_finalize_headless()` 寫入 `registry.update_headless_result`（job status），
上層 `manager.py` 的 recovery 邏輯（`retry-build`／`abandon` 等 human recovery
action，對應 `openspec/specs/trusted-dispatch-completion/spec.md` 的
「Human recovery必須明確且可追蹤」需求）需要新增對 `context-exhausted` 狀態的
分支：與「零 commit 的一般 failed」不同，`context-exhausted` 且有部分 commit
（`dispatch_head` != 當前 branch head）時，正確恢復動作是**分段續跑**（見
D1／D5），而非直接判給 verification/needs_human 或無腦 `retry-build`（那會
重新從 dispatch base 派整份 plan，重演同一個 context 陣亡）。

### D5 續跑進度帳最小可行方案：commit log 作帳本，不新增 plan checkbox 回寫

比較兩個選項：

- **選項 A（commit log 即帳本）**：續跑時比對 `git log --oneline <branch>`
  與 D2 `list_plan_tasks()` 回傳的 Task 順序，把「已有對應 commit」的 Task
  視為完成，續派下一個未完成的 Task；續跑 prompt 附「已完成 commits 摘要」
  供 agent 讀取上下文。
- **選項 B（plan checkbox 回寫）**：builder session 在 worktree 內編輯
  `docs/superpowers/plans/*.md` 把完成的 Task checkbox 打勾並 commit，
  manager 讀 checkbox 狀態判斷進度。

採選項 A。理由：選項 B 要求 builder 在「本段結束前必須 commit」的紀律之外，
再多負擔一項「還要記得改 plan 檔」的責任，且 checkbox 回寫本身也是一次
commit——如果 context 剛好在改 checkbox 前耗盡，帳本又出現「commit 有了但
checkbox 沒打勾」的不一致視窗，反而製造新的競態。選項 A 用**已經是唯一可信
真相源**的 branch commit history 判斷進度，不需要 builder 額外配合，且與
D1 的 `dispatch_head` baseline 機制天然一致（baseline 前進＝該段有新
commit＝該 Task 視為完成的必要條件；是否*充分*——commit 內容是否真的對應
該 Task——本設計不強求機械證明，留給既有的 verification／review 流程把關，
帳本只用來決定「續派哪個 Task」，不取代 candidate 驗證）。

最小可行實作：`list_plan_tasks()` 回傳的 `TaskUnit` 序列 + `git log
--oneline <branch> <dispatch_base>..HEAD` 的 commit 數量，用**「已派工過的
TaskUnit 數」**（而非逐一比對 commit message 與 Task 內容語意）作為完成
游標——即「第 N 個 TaskUnit 派工後產生了新 commit（baseline 前進）」就視為
第 N 個 Task 完成，續派第 N+1 個。這個游標本身應該落地為 job/slice 層級的
持久狀態（例如 job row 新增 `task_index` 欄位），而非每次即時重新解析 plan
與 git log 推算——避免 plan 文字被後續 edit 改動（例如 reviewer 建議調整
Task 順序）時，游標跟著漂移。

### D6 與 #277（completion 快照競態）的邊界：共用同一段 recovery code path，D4 是 #277 的前置分類輸入

#277 處理的是「exit 0 但 completion 端拿不到快照」的競態（manager 判斷
readiness 的時間點與 candidate 實際落地的時間點之間的 race）；D4 處理的是
「exit code 是什麼、jsonl 內容說了什麼」的**分類**問題。兩者在 issue #276
原文「issue-9-coverage-truth-build」案例交會：`exit 0` 但**未 commit**——
這既是 D4 要標記的 `context-exhausted`（jsonl 含目標字串或末筆判定），也是
#277 要處理的「completion 端看不到 candidate」情境的一種成因（這裡的成因是
「builder 真的沒 commit」而非「commit 了但快照沒捕捉到」，兩者外觀相似但
根因不同）。

介面契約：D4 的 `classify_completion()` 回傳值是 #277 recovery 邏輯的**輸入
之一**，不是取代關係——#277 的快照競態偵測邏輯應該在 `classify_completion()`
判定完 `exited`/`failed`/`context-exhausted` **之後**才介入判斷「該終局狀態
下 candidate 快照是否可信」，避免兩票各自在 `_finalize_headless()` 或
`manager.py` recovery 分支插入互相打架的邏輯。本設計票不實作 #277，只在此
記交會點：**後續哪一票先落地都不得繞過對方的分類輸出**，若 D4 的 code 票
先落地，`context-exhausted` 必須是 #277 recovery 邏輯後續可以消費的既有列舉
值之一，不能讓 #277 自己另開一套獨立於 `classify_completion()` 的判斷。

## 風險與緩解

- **`redispatch()` 與 `dispatch()` 兩條路徑分岔維護成本**：新增方法而非改
  既有簽名，向後相容邊界清楚（未分段 slice 完全走舊路徑）；緩解漂移風險的
  作法是兩者共用 `_pane_sender.send()`／`registry.create_job()` 底層呼叫，
  只有 worktree 建立與 baseline 取得邏輯不同。
- **`context-exhausted` 全檔掃描字串比對脆弱**（executor CLI 改錯誤文案即
  失效）：本設計明文接受這是已知限制——這條規則綁定 issue #276 實測到的
  具體字串，未來若 codex CLI 改變錯誤訊息格式，偵測會退化回一般 `failed`
  （fail-closed 到現行行為，不會誤判成別的狀態），非 silent breakage。
- **commit-count 游標與 plan 編輯順序漂移**（D5）：游標落地為持久狀態而非
  即時重算，緩解 plan 文字事後編輯造成的錯位；若後續需要更強的語意對齊
  （commit message 與 Task 內容比對），留待有實測需求時再開票，本票不
  過度設計。
- **D4／D6 邊界文字約束力有限**（下一位實作者仍可能各自為政）：緩解作法是
  code 票落地時，`context-exhausted` 的 enum 值與其在 `_finalize_headless`／
  `manager.py` 的消費點必須在 PR 描述明確引用本設計文件的 D6 段落，並在
  `openspec/specs/trusted-dispatch-completion/spec.md` 的 Requirement 內
  明文兩票的介面契約（見對應 spec delta）。
