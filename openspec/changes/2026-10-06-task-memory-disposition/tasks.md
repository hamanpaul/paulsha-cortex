---
status: accepted
work_item: task-memory-disposition
---

# Tasks

- [x] **T1 RED**：新增測試，固定以下行為（現行應失敗）：
      - 有送記憶的 attempt，terminal schema 要求 `task_memory_disposition`。
      - Manager 驗證 note_id 集合等於實際送出的集合。
      - 缺欄位、格式錯、集合不符時記為 `unreported` 並附原因，卡片判定與沒有此欄位時完全相同。
      - verdict 只接受 `applied`／`consulted_no_change`／`not_relevant`／`stale_or_wrong`／`already_known`／
        `not_read`；reason 必填且 ≤140 字。
- [x] **T2 schema 與 prompt**：只在有送記憶時，在各 executor 的 terminal schema 與 terminal 說明文字加入
      `task_memory_disposition`，並列出這次送出的 note_id，讓 model 逐則回報。
- [x] **T3 Manager 驗證與 receipt**：
      - 在 `_extract_terminal_payload` 拆出新欄位；跟 `task_memory_applied` 一樣在形狀驗證之前拆掉，不影響
        卡片採信。
      - harvest 時驗證 note_id 集合，寫入新的 receipt 事件 `disposition-reported`，內容含每則 verdict 與 reason
        的 sha256。reason 原文是否保存要在文件寫明。
      - 驗證失敗時寫入 `unreported` 事件並附原因。
- [x] **T4 applied 證據放寬**：verification／code-review 卡的 applied 證據，可以指向該卡 diagnostics 中的
      finding key，不限 Candidate 已 commit 的檔案。worktree-isolation 這類不產生 commit 的卡，仍可回報
      applied 以外的 verdict。
- [x] **T5 送達查證（選擇性，做得到就做）**：host 端記錄送出記憶區塊的 sha256 並寫進 receipt，讓 claude 卡
      等不回顯 prompt 的 executor 也能查證送達。做不到時在 PR 寫明原因。
- [x] **T6 指標與文件**：
      - 提供統計填寫率、verdict 分布與 unreported 原因的方式（CLI，或寫在文件中的查詢方法）。
      - 更新 task-memory 文件。
      - 新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
