---
status: draft
work_item: builder-task-boundary-segmentation
---

## Goals

定案「builder 派工依 plan Task 邊界分段」的架構決策（分段執行模型、Task
邊界解析、prompt 模板改動、completion 分類擴充、續跑進度帳），作為後續
code 票的單一設計依據，解決 #276 核心問題：整份 plan 塞進單一 session 的
builder 派工在有限 context window 的 executor 上結構性陣亡。

## Why

2026-07-30～2026-08-06 dts-build issue-14 批次 dogfood 實測三個 slice 同一晚
全部因 context 耗盡陣亡（exit 0 未 commit、或 exit 1 零 commit），改成「依
plan Task 邊界分段派工＋反漫遊紀律入 prompt」後同一批 work item 全數通過
（existence proof，見 #276 原文與 `docs/superpowers/specs/
builder-task-boundary-segmentation-spec.md` 背景段）。這是結構性問題，與
特定模型無關，任何有限 context 的 executor 都會踩到。核實 main 現況：
`contract_command.py:build_dispatch_prompt()`、`dispatcher.py:
Dispatcher.dispatch()`、`completion.py:classify_completion()` 三者皆完全
未涵蓋分段語意；唯一可重用的既有元件 `planning.py:_collect_task_items()`
只做 plan review 完整性檢查、未接到派工路徑且回傳值攤平無法回推 Task 邊界。

範圍橫跨 dispatcher（fan-out 單位）、manager_daemon（tick 迴圈）、
contract_command（prompt 模板）、completion（終態分類 enum）、續跑帳本
schema 四個不同子系統，且與 #277（completion 快照競態）在「exit 0 未
commit」情境交會，需要先做架構決策再拆碼票——比照本 repo 對同量級票
（如 #294→feat-slice-executor-model、#139→design-task-type-taxonomy-v2）
一律先出 proposal/design/spec 三件套的既有慣例，故本票只交付設計文件，
不動 `paulsha_cortex/` 任何程式檔。

## What Changes（設計層級，非程式碼變更）

- 定案 D1：per-Task fan-out 是 manager tick 迴圈對同一 slice 反覆呼叫
  `Dispatcher` 新增的「同 worktree 續派」原語，不擴充現有
  `Dispatcher.dispatch()`（該方法必建新 worktree，第二個 Task 起會與既有
  worktree 目錄衝突）。
- 定案 D2：新增 `planning.list_plan_tasks()` 回傳按 `## Task N` 分段的
  `TaskUnit` 序列，與現有 `_collect_task_items()`（攤平、僅供完整性檢查）
  並存不互相取代。
- 定案 D3：`build_dispatch_prompt()` 新增 optional `task_slice` 參數，未傳
  維持現行整份 plan 行為（向後相容）；傳入時嵌入反漫遊紀律與段尾 commit
  斷點語句。
- 定案 D4：`classify_completion()` 新增 `context-exhausted` 第三態，偵測
  jsonl 內 `ran out of room in the model's context window` 字串，貫穿
  `dispatcher.py`／`manager.py` 的 recovery 邏輯，區分「有部分 commit 可
  續跑」與「零 commit 需人工判斷」。
- 定案 D5：續跑進度帳採 commit log／`dispatch_head` baseline 前進作完成
  游標（持久化於 job/slice 層），不採 plan checkbox 回寫。
- 定案 D6：與 #277（completion 快照競態）的邊界——D4 的分類輸出是 #277
  recovery 邏輯的輸入之一，兩票不得各自實作互相打架的獨立判斷路徑。
- 不實作 D1-D5 任何一項；code 落地留給依此設計拆分的後續 issue（見
  `tasks.md` 文末的拆票建議）。

## Capabilities

### Modified Capabilities
- `trusted-dispatch-completion`：詳見 `specs/trusted-dispatch-completion/spec.md`
  的 Requirements delta，與 `docs/superpowers/specs/
  builder-task-boundary-segmentation-spec.md` 的完整 Requirements、
  `docs/superpowers/specs/builder-task-boundary-segmentation-design.md`
  的 Decisions。
