---
status: draft
work_item: builder-task-boundary-segmentation
---

# builder-task-boundary-segmentation Specification

#276：builder 派工目前是「整份 plan 塞進一個 session」，在有限 context window
的 executor 上結構性陣亡——探索與實作全擠在同一個 session，context 耗盡時
整場工作歸零（builder 從不中途 commit）。本 spec 定案「依 plan Task 邊界分段
派工、每段乾淨 context、段尾強制 commit」的契約，作為後續拆分 code 票的
Requirements 依據。

## 背景

2026-07-30～2026-08-06 dts-build issue-14 批次 dogfood（cortex 0.1.1、systemd
daemon、builder identity codex／gpt-5.3-codex-spark）同一晚三個 work item 的
實測：issue-9-coverage-truth-build exit 0 但未 commit（7.39M input tokens
後在收尾前把 context 用完）；issue-15／issue-12 第一輪皆 exit 1、零 commit
零 diff，jsonl 尾為 `Error running remote compact task: Codex ran out of room
in the model's context window.`。改成「依 plan Task 邊界分段派工＋反漫遊紀律
入 prompt」後，同一批 work item 全數通過（issue-12 五段全過五個乾淨 commit；
issue-15 第二輪單段但反漫遊紀律奏效）。

現況缺口（皆已在 main 上核實，見對應 D 決策的檔案:行號）：

- `paulsha_cortex/coordinator/contract_command.py:9-28` 的 `build_dispatch_prompt()`
  只能整份 plan 引用，無 per-Task 邊界、無反漫遊紀律語句、無逐 Task commit
  要求（見 D3）。
- `paulsha_cortex/coordinator/dispatcher.py` 的 `Dispatcher.dispatch()`
  （108-138 行）一個呼叫對應一個 worktree／一個 job，沒有「同 worktree、
  fresh context、逐 Task 呼叫」的分段迴圈（見 D1）。
- `paulsha_cortex/coordinator/completion.py:60-69` 的 `classify_completion()`
  只有 `exited`／`failed` 兩態，不解析 `ran out of room in the model's
  context window` 字串（見 D4）。
- 無任何續跑進度帳機制（見 D5）。
- `paulsha_cortex/coordinator/planning.py:317-349` 的 `_collect_task_items()`
  已能解析 plan body 的 `## Task N` heading 結構，但回傳值攤平、無法回推
  Task 邊界，目前只用於 plan review 完整性檢查（見 D2）。

## Goals

- builder 派工能以 plan 的 `## Task N` 邊界分段，每段 fresh context、段間以
  commit 為斷點，取代現行單一整份 plan session 的結構性 all-or-nothing。
- prompt 模板明文反漫遊紀律與段尾 commit 要求，未宣告分段的既有 slice 行為
  不變（向後相容）。
- completion 分類能辨識 context-exhaustion 這個特定失敗模式，讓 recovery
  邏輯可以選擇「續跑」而不是無差別「換人重來」。
- 續跑要有進度真相源，避免重複執行已完成的 Task。

## Requirements

### R1 per-Task fan-out（對應 D1）

manager tick 迴圈 SHALL 能對同一 slice 逐 Task 反覆派工：偵測前一個 Task
segment 的 builder job 已終局（`exited`／`failed`／`context-exhausted`）且
仍有未完成 Task 時，SHALL 以同一 worktree／branch 開新 job row 派下一個
Task，不得重新呼叫會嘗試重建 worktree 的既有 `Dispatcher.dispatch()` 路徑
（`seams.py:70-76` 的 `GitWorktreeCreator.create()` 對已存在的 worktree 目錄
raise，直接重用會炸掉第二個 Task 起的派工）。每個 Task segment 的
`dispatch_head` baseline SHALL 取該 segment 派工當下的 branch head（非第一
個 Task 的舊 baseline），確保完成偵測（`poll_done`）判斷的是「這一段」而非
「累積至今」有無新 commit。

若不做：issue-12 六個 Task 的 plan 仍只能整份塞單一 session，重現 issue
#276 實測的 context 陣亡案例（issue-9／issue-12／issue-15 第一輪）。

### R2 prompt 模板反漫遊與 commit 斷點語句（對應 D3）

`build_dispatch_prompt()` SHALL 新增 optional 參數（該 Task 的內容），未傳
時 SHALL 與現行行為逐位元一致（第 27 行整份 plan 引用字面不變）。傳入時
prompt SHALL 只嵌該 Task 內容，並 SHALL 附加「plan 已決策完備、禁止長時間
探索／全庫漫遊」與「本段結束前 git status 必須乾淨、必須完成至少一個
commit」兩段明文語句。

若不做：即使做到 R1 的分段派工，缺少反漫遊紀律的段落仍可能像 issue-9／
issue-12 第一輪那樣把整段 context 耗在探索上而非動手實作——issue #276
「已驗證有效的對策」明載這是與分段本身並列的第二個變因。

### R3 completion 新增 `context-exhausted` 分類（對應 D4）

`classify_completion()` SHALL 新增第三態 `context-exhausted`：當 jsonl 內容
（不限末筆）含 `ran out of room in the model's context window`（大小寫不
敏感）時 SHALL 回傳該分類，優先權高於既有 `exited`／`failed` 判定（即使
exit code 為 0）。現行只看末筆 JSONL 的 `obj.get("ok") is False` 判定 SHALL
保留為 `failed` 分類的既有路徑，不得被新分類取代。

若不做：`completion.py:60-69` 目前對 issue #276 引用的錯誤字串必然 fallback
落回 `failed`（末筆非該錯誤訊息時甚至可能落回 `exited`），后续 code 票寫
`classify_completion` 對含該字串的 jsonl 回傳 `context-exhausted` 的 RED
test 會失敗於現行行為——此字串目前必然落回 `failed` 或 `exited`，無法與
「單純模型能力不足的一般失敗」區分，recovery 邏輯無法選擇正確恢復動作。

### R4 續跑進度帳（對應 D5）

分段派工 SHALL 有續跑判斷依據：以「已派工過的 TaskUnit 數」對應「該 Task
segment 派工後 branch head 是否已相對其 baseline 前進」作為完成游標，
SHALL 落地為 job／slice 層級持久狀態（不即時重新解析 plan＋git log 推算），
避免 plan 文字事後編輯造成游標與人類可讀 Task 編號錯位。SHALL NOT 引入
plan checkbox 回寫作為帳本（builder 需額外負擔記得改 plan 檔且該 commit
本身可能又落在 context 耗盡前，製造新的不一致視窗）。

若不做：分段派工的續跑（例如 daemon 重啟後、或某段 `context-exhausted`
後）無法判斷哪些 Task 已完成，要嘛重跑全部 Task（浪費已完成的工作與
token），要嘛需要 operator 手動判斷——與 issue #276「續跑的進度帳」對策
項目直接對應。

### R5 向後相容：未宣告分段的既有 slice 行為不變

R1-R4 的所有改動 SHALL 為 additive：未使用分段派工的既有 slice／spec，
其 dispatch 全路徑（prompt 內容、`Dispatcher.dispatch()` 呼叫方式、
`classify_completion()` 對非 context-exhausted 字串的 jsonl 判定）SHALL
與現行為位元一致。`context-exhausted` 是既有 `exited`／`failed` 二元列舉
的擴充而非取代，既有只認得 `exited`/`failed` 的呼叫端在未升級前 SHALL
至少 fail-closed 落回 `failed`（不得因未知第三態而拋未處理例外或誤判成
`exited`）。

若不做：分段機制上線後既有未分段 slice 若行為改變，等同引入迴歸——這與
issue #276「明確不在本次範圍」段落（不處理 planner／reviewer persona 的
類似問題，只針對 builder；不處理 executor CLI 自身的 context 管理）並列的
隱含前提一致：本次變更是新增能力，不是重寫既有派工路徑。

## 非目標

- 各 executor CLI 自身的 context 管理（compact／續 thread）——cortex 只需
  把失敗辨識出來並提供結構性斷點（issue #276 原文「明確不在本次範圍」）。
- planner／reviewer persona 的類似問題（本次只實測到 builder）。
- #277（completion 快照競態）的偵測與恢復邏輯本身——D6 只記交會點與介面
  契約，不在本票實作；`context-exhausted` 分類是 #277 recovery 邏輯的
  輸入之一，不是取代關係。
- plan checkbox 回寫、任何需要 builder 額外配合維護的續跑帳本形式（R4
  已明文排除，理由見對應 D5）。
- combo／cards.yaml 的 `commit_policy` 語意本身不改；per-Task commit 斷點
  是 prompt 層新增的段間要求，不改變 slice 終局的 `commit_policy: required`
  既有語意（`paulsha_cortex/deck/data/cards.yaml:76,93`）。

## 驗收面

- R1-R4 分別可對應到 main 上至少一個具體檔案／函式作為改動錨點（見各 R
  段落），且皆可指出「若不做會重現 issue #276 三個 slice 陣亡案例之一」。
- R3 的字串偵測依據明確為 `ran out of room in the model's context window`，
  且與 `completion.py:60-69` 現況（只有 `exited`／`failed`）的落差可直接
  轉譯為後續 code 票的 TDD RED test（對含該字串的 jsonl，現行
  `classify_completion` 必然不回傳 `context-exhausted`）。
- R5 的向後相容邊界清楚到可以寫成一條回歸測試：未傳 `task_slice` 的
  `build_dispatch_prompt()` 呼叫，輸出與變更前逐位元相同。
