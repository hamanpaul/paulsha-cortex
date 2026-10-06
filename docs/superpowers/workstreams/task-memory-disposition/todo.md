---
status: accepted
work_item: task-memory-disposition
---

# task-memory：每則送出的 note 必填處置與原因（task_memory_disposition）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1309`。owner 2026-10-06 同意，需求來自 Hippo 端（Hippo×JEV 是否進 Q2，
  要靠這份資料判斷）。
- 現況（#1136）：terminal 有選填欄位 `task_memory_applied`，只回報正面結果。applied 必須附 Candidate 中
  已 commit 檔案的路徑與 sha256。相關程式：
  - `paulsha_cortex/coordinator/task_memory.py`：`TASK_MEMORY_APPLIED_TERMINAL_FIELD`、
    `task_memory_applied_json_schema`、`parse_task_memory_applied`。
  - `paulsha_cortex/coordinator/launcher.py`：terminal schema 注入。
  - `paulsha_cortex/coordinator/manager.py`：`_extract_terminal_payload` 拆出選填欄位、
    `_harvest_task_memory_applied` 寫入 receipt、給 model 的 terminal 說明文字。
  - 測試：`tests/test_task_memory_applied_evidence_1136.py`。
- 不變的原則（沿用 #1136）：此欄位的任何問題（缺漏、格式錯、集合不符）都**不得**讓卡片失敗，也**不得**改變
  卡片判定；只記錄在 task-memory 的 receipt 與指標上。
- 只有當次 attempt 實際送了 task-memory note 時才要求此欄位；沒送記憶的 attempt 行為完全不變。
- 既有的 `task_memory_applied` 照舊可解析；新舊欄位並存時的優先順序要寫明並測試。

## Tasks

- [x] **T1 RED**：新增測試，固定以下行為（現行應失敗）：
      - 有送記憶的 attempt，terminal schema 要求 `task_memory_disposition`。
      - Manager 驗證 note_id 集合等於實際送出的集合。
      - 缺欄位、格式錯、集合不符時記為 `unreported` 並附原因，卡片判定與沒有此欄位時完全相同。
      - verdict 只接受 `applied`／`consulted_no_change`／`not_relevant`／`stale_or_wrong`／`already_known`／
        `not_read`；reason 必填且 ≤140 字。
- [ ] **T2 schema 與 prompt**：只在有送記憶時，在各 executor 的 terminal schema 與 terminal 說明文字加入
      `task_memory_disposition`，並列出這次送出的 note_id，讓 model 逐則回報。
- [ ] **T3 Manager 驗證與 receipt**：
      - 在 `_extract_terminal_payload` 拆出新欄位；跟 `task_memory_applied` 一樣在形狀驗證之前拆掉，不影響
        卡片採信。
      - harvest 時驗證 note_id 集合，寫入新的 receipt 事件 `disposition-reported`，內容含每則 verdict 與 reason
        的 sha256。reason 原文是否保存要在文件寫明。
      - 驗證失敗時寫入 `unreported` 事件並附原因。
- [ ] **T4 applied 證據放寬**：verification／code-review 卡的 applied 證據，可以指向該卡 diagnostics 中的
      finding key，不限 Candidate 已 commit 的檔案。worktree-isolation 這類不產生 commit 的卡，仍可回報
      applied 以外的 verdict。
- [ ] **T5 送達查證（選擇性，做得到就做）**：host 端記錄送出記憶區塊的 sha256 並寫進 receipt，讓 claude 卡
      等不回顯 prompt 的 executor 也能查證送達。做不到時在 PR 寫明原因。
- [ ] **T6 指標與文件**：
      - 提供統計填寫率、verdict 分布與 unreported 原因的方式（CLI，或寫在文件中的查詢方法）。
      - 更新 task-memory 文件。
      - 新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
