---
status: draft
work_item: builder-task-boundary-segmentation
---

## MODIFIED Requirements

### Requirement: Execution與delivery狀態必須分離

系統MUST以versioned atomic coordinator state分別保存Job execution與Slice delivery。exit code 0只能令Job進入`exited`，不得直接產生CompletionRecord、`completed` Slice或滿足`depends_on`。系統MUST拒載legacy或未知schema且不得自動清空或migration。Job execution終局狀態MUST能區分「一般failed」與「context窗口耗盡導致的failed」（`context-exhausted`）：偵測依據為dispatch jsonl內含`ran out of room in the model's context window`字串（大小寫不敏感），優先權高於既有exit code與末筆JSONL `ok`欄位判定——即使exit code為0，只要偵測到該字串即MUST標記為`context-exhausted`而非`exited`。`context-exhausted`MUST為既有`exited`/`failed`二元列舉的additive擴充，未識別該分類的既有呼叫端MUST至少fail-closed落回`failed`，MUST NOT因未知第三態拋未處理例外或誤判為`exited`。

#### Scenario: Agent成功退出但尚未驗證
- **WHEN** builder Job以exit code 0結束
- **THEN** Job成為`exited`且Slice進入verification path
- **THEN** downstream維持blocked

#### Scenario: 啟動遇到legacy state
- **WHEN** coordinator讀到缺少支援schema version或含legacy `done`的`jobs.json`
- **THEN** coordinator拒絕啟動並顯示state路徑與archive/remove指引
- **THEN** 原state檔保持不變

#### Scenario: 使用舊低階direct dispatch
- **WHEN** operator嘗試使用沒有spec/plan/verification metadata的legacy direct dispatch介面
- **THEN** CLI明確拒絕並指示使用spec-driven control request
- **THEN** 系統不寫Job、Slice或CompletionRecord

#### Scenario: Daemon未運行時要求mutation
- **WHEN** operator呼叫`dispatch/fanout/tick/complete/slice-action`且沒有manager daemon可消費control request
- **THEN** CLI以明確錯誤結束且不直接寫`jobs.json`

#### Scenario: exit 0但context耗盡發生於收尾前
- **WHEN** builder Job以exit code 0結束，但dispatch jsonl內含`ran out of room in the model's context window`
- **THEN** Job標記為`context-exhausted`而非`exited`
- **THEN** Slice不因exit code 0進入verification path，等同一般failed的阻擋語意，但保留可與零commit的一般failed區分的分類供recovery邏輯判斷

#### Scenario: 未升級的呼叫端讀到context-exhausted
- **WHEN** 尚未支援`context-exhausted`分類的既有呼叫端讀到該狀態值
- **THEN** MUST至少fail-closed處理為`failed`
- **THEN** MUST NOT因無法識別該值而拋未處理例外，MUST NOT誤判為`exited`

## ADDED Requirements

### Requirement: builder派工MUST能依plan Task邊界分段

manager MUST能對同一Slice在同一worktree內逐Task反覆派工：偵測前一個Task segment的builder Job已終局（`exited`／`failed`／`context-exhausted`）且plan仍有未完成Task時，MUST以同一worktree／branch開新Job row派下一個Task，MUST NOT呼叫會嘗試重建該worktree目錄的既有派工路徑（該路徑對已存在的worktree目錄fail-closed拒絕，重用會導致第二個Task起的派工全部失敗）。每個Task segment的完成判定baseline MUST取該segment派工當下的branch head，MUST NOT沿用先前Task的舊baseline。未使用分段的既有Slice MUST完全不觸發此派工路徑，行為位元不變。

#### Scenario: 單一slice逐Task分段派工

- **WHEN** 某Slice的plan含多個`## Task N` heading，第一個Task segment的builder Job已終局且產生新commit
- **THEN** manager以同一worktree／branch開新Job row派下一個Task
- **THEN** 新Job的完成判定baseline為當下branch head，非第一個Task的原始baseline

#### Scenario: 未分段slice不受影響

- **WHEN** 某Slice的派工未使用Task邊界分段
- **THEN** 派工行為與變更前完全一致，不觸發同worktree續派路徑

### Requirement: builder派工prompt MUST能表達單一Task範圍與反漫遊紀律

builder派工prompt生成MUST支援optional的Task範圍參數；未提供時MUST與現行整份plan引用行為逐位元一致。提供時prompt MUST只嵌該Task內容，MUST附加明文語句要求「plan已決策完備、禁止長時間探索或全庫漫遊」與「本段結束前git status必須乾淨、必須完成至少一個commit」。

#### Scenario: 未提供Task範圍時行為不變

- **WHEN** prompt生成呼叫未提供Task範圍參數
- **THEN** 輸出與變更前逐位元相同，包含既有整份plan引用字面

#### Scenario: 提供Task範圍時嵌反漫遊與commit斷點語句

- **WHEN** prompt生成呼叫提供單一Task範圍
- **THEN** prompt只嵌該Task內容，不嵌整份plan其餘Task
- **THEN** prompt MUST含反漫遊紀律語句與段尾commit斷點語句
