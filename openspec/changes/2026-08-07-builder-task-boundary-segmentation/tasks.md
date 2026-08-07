---
status: draft
work_item: builder-task-boundary-segmentation
---

# Tasks

design-doc 票，非 code TDD RED/GREEN；驗收為文件三件套完整＋經至少一輪
review，比照 `2026-07-30-terminal-result-contract`／
`2026-08-04-design-task-type-taxonomy` 等既有 design-doc 票的慣例。

- [ ] 1.1 `proposal.md`／`design.md`／`specs/trusted-dispatch-completion/spec.md`
      三件套完整，且與
      `docs/superpowers/specs/builder-task-boundary-segmentation-design.md`／
      `-spec.md` 內容一致（openspec 三件套為摘要、docs/superpowers 為完整
      論證，兩者不得互相矛盾）。
- [ ] 1.2 `docs/superpowers/specs/builder-task-boundary-segmentation-spec.md`
      的 R1-R5 逐條可對應到「若不做會重現 issue #276 三個 slice 陣亡案例
      之一」，且每條皆指出對應 D 決策與至少一個 main 上現有檔案／函式作為
      改動錨點（非空泛陳述）。
- [ ] 1.3 D4（context-exhausted 分類）附上 issue 原文引用的錯誤字串
      `ran out of room in the model's context window` 作為偵測依據，並指明
      `completion.py:60-69` 現況（只有 `exited`/`failed`）作為「改前」對照。
- [ ] 1.4 本設計文件經至少一輪 review（人工或 reviewer persona）才可勾完
      此清單；不可自我勾完就 claim done。
- [ ] 1.5 `changelog.d/builder-task-boundary-segmentation-design.md` fragment
      與 `CHANGELOG.md [Unreleased]` entry（#276）。
- [ ] 1.6 `python3 -m pytest -q` 全綠（docs-only 變更，不應影響既有測試）；
      帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

三件套（openspec proposal/design/spec）與 docs/superpowers 完整文件皆存在
且互相一致；R1-R5 皆可證偽（每條指出改動錨點與「不做的後果」）；D6 明確
記錄與 #277 的交會點與介面契約，不得被後續任一票繞過；不動
`paulsha_cortex/` 任何程式檔。

## 後續應拆分的 code 票（建議，非本票範圍）

設計文件本身也要遵守 Task 邊界紀律——以下拆法避免單票過大重演 issue #276
描述的 context 陣亡問題本身：

1. **核心分段機制**（D1+D2+D3 最小組合，issue #276 建議範圍第一項）：
   - `planning.list_plan_tasks()` 新函式（D2）。
   - `Dispatcher` 新增同 worktree 續派方法，manager tick 迴圈接入逐 Task
     反覆派工（D1）。
   - `build_dispatch_prompt()` 新增 optional `task_slice` 參數＋反漫遊／
     commit 斷點語句（D3）。
   - 驗收：既有未分段 slice 行為位元不變（回歸測試）；`list_plan_tasks()`
     對含多個 `## Task N` heading 的 plan 正確切分；同一 slice 連續兩次
     派工不因 worktree 已存在而失敗。
   - 這張票落地後，D1 的「有限 context executor 結構性陣亡」核心問題即
     已解掉，即使 D4／D5 尚未跟進。
2. **completion 分類擴充**（D4，issue #276 建議範圍第二、三項）：
   - `classify_completion()` 新增 `context-exhausted`；`_finalize_headless`／
     `manager.py` recovery 邏輯區分「部分 commit 可續跑」與「零 commit 需
     人工判斷」。
   - 依賴：可獨立於票 1 開發測試（用 fixture jsonl 直接測
     `classify_completion`），但 recovery 邏輯的「續跑」動作依賴票 1 的
     `redispatch()` 存在才有意義。
   - 需遵守 D6：與 #277 recovery 邏輯的介面契約，落地時 PR 描述須引用本
     設計文件 D6 段落。
3. **續跑進度帳**（D5，issue #276 建議範圍第四項）：
   - job/slice 層新增 `task_index` 持久欄位，續跑時據以決定續派哪個
     Task。
   - 依賴票 1（需要 `TaskUnit` 序列與 `dispatch_head` baseline 機制）。

三張票各自可獨立驗收、獨立 review，符合本 repo「不做超過單一 context
能消化範圍」的既定教訓（issue #276 本身即是這個教訓的來源）。
