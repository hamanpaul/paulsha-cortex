---
status: accepted
work_item: monitor-canonical-todo-qualification
owner_issue: 1063
parent_issue: 1054
domain_breadth: 1
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
  - R-22
---

# Monitor canonical Todo qualification 規格（#1063）

Owner 是 live [#1063](https://github.com/hamanpaul/paulsha-cortex/issues/1063)，parent/consumer 是 [#1054](https://github.com/hamanpaul/paulsha-cortex/issues/1054)。本文件只凍結 Monitor source qualification 與一般 path-link admission contract；不包含 Manager first-Builder gate。

## Requirements

### R1 — 可計數 Todo 必須符合 canonical workstream path

唯一可作為 #1054 admission 輸入的 Todo path MUST 精確符合 `docs/superpowers/workstreams/<slug>/todo.md`，其中 `<slug>` 是單一路徑段並符合 `[a-z0-9][a-z0-9-]*`。巢狀子目錄、其他目錄中的 todo.md、大小寫變體與名稱相似的 spec/design/plan 均 MUST NOT 成為 qualified Todo。

Todo 必須是受監控 repository root 內的 regular file。檢查路徑各段時不得跟隨 symlink；source revision 和解析結果 MUST 來自同一份已讀取 bytes，避免 path/content race。

### R2 — Issue provenance 與 work-item correlation 必須同時吻合

Todo frontmatter MUST 是 YAML mapping，並提供：

- issue：正整數 YAML scalar；bool、字串、零、負數及缺值均拒絕。
- work_item：符合 [a-z0-9][a-z0-9-]* 的非空 scalar。

Monitor 只有在 source 經 kind:path 明確連到同一 (repo, work_id)，work_item 逐字等於該 work_id，且 issue 等於同一 WorkAuthority 的 confirmed GitHub issue 編號時，才能標示來源為 qualified。issue 必須在同一 repo；其他 work item 的同號 issue、同 work item 的其他 repo issue、frontmatter 字串巧合均不成立。

### R3 — Tasks 使用 deterministic、可測的非 placeholder 格式

Tasks section MUST 是唯一的二級標題 `## Tasks`。解析範圍到下一個標題或檔案結尾；範圍中的每一個非空白行 MUST 符合下列單行格式之一，不接受巢狀 task、其他 checkbox 寫法或任意說明行：

```markdown
- [ ] **T1 Add a test**: Add a regression test and assert the rejected path leaves override bytes unchanged.
- [x] **T2 Record source revision**: Compute the revision from the same bytes used for parsing.
```

`T<n>` 的 `n` 是正整數；同一 Todo 的 task ID 不得重複。title 與 action 都必須 trim 後非空，且不得單獨為 TODO、TBD、placeholder、N/A、none、later 或 coming soon（大小寫不敏感）。section 至少要有一筆格式正確的 task，且至少一筆為未完成 `[ ]`；所有非空白行都必須符合格式。缺 heading、重複 heading、空 section、只有已完成項目、格式錯誤或 placeholder 都使 Tasks validation 失敗。格式可機械驗證；action 的工程可行性仍由規劃審查判斷，parser 不宣稱能做語意認證。

此格式驗證保證有命名工作與 action 欄位，不聲稱 parser 能證明文字語意可行或驗證作者身份。

### R4 — Qualification result 保留可核對來源與驗證結果

每個掃描到的 canonical Todo 都 MUST 保留 typed qualification outcome，包含 schema/version、source ID、repo-relative path、source revision、frontmatter issue、frontmatter work_item、Tasks validation（格式版本、有效 task 數、pending task 數、失敗 reason codes）與 qualified/rejected 結果。

WorkAuthority MUST 另提供 qualified_todos typed collection，僅包含 R1–R3、同 WorkAuthority path ownership 都通過的 records。record 的 path/revision 必須與對應 WorkSource 完全一致。不得由 Manager 以 basename、regex 對 source 字串或 checkbox 自行重推 qualification。

既有 raw mapped_todo_paths 與 confirmed_todo 欄位維持原語意，供現有消費者相容；#1054 的 Todo count 只能使用 qualified_todos。不合格但已 linked 的 Todo 可以保留在一般 source inventory，不能進入此 collection。

### R5 — Owner-published 是可觀測的 repository correlation，不是作者認證

本契約把 owner-published 操作化為：Todo 位於受監控 repo 的 R1 canonical path、其 R2 issue 與 work_item 對應同一 WorkAuthority、且 path link 明確屬於該 work item。Monitor MUST NOT 宣稱 glob、checkbox、Git commit author 或 frontmatter 能證明 GitHub 帳號、簽章或真人身份。

### R6 — 一般 path link 只接受現存、安全且可掃描的 source

cortex work link <work_id> --repo <owner/repo> --kind path --ref <repo-relative-path> MUST 在寫入 override 前驗證 target：

1. 是 canonical repo-relative POSIX path，沒有 absolute、.. 或正規化歧義。
2. 已存在於 repo root 之下，且各 path component 都不是 symlink，最終 target 是 regular file。
3. 符合 Monitor 目前支援的 path scanner roots：workstream todo.md、docs/superpowers/specs/**/*.md 或 docs/superpowers/plans/**/*.md。
4. 透過與 Monitor correlation 共用的 validator 通過；檢查失敗時 override bytes 不得改變。

這是一般 source link gate，不驗證 Todo 的 issue/work_item/Tasks qualification。既有 spec、design、plan path link 用途 MUST 保留。OpenSpec change 仍使用既有 kind:openspec link。

### R7 — Qualification 與 path link failure 均 fail closed

缺檔、非 regular file、symlink traversal、repo escape、非 scanner path 或 canonical path 不符時，kind:path link MUST 拒絕且零 override mutation。

Todo 的 frontmatter/Tasks/WorkAuthority mismatch MUST 產生不可計數的 rejected qualification；不得因 kind:path link 已存在而提升為 qualified。失敗 metadata 可留在診斷中，但不得回顯絕對路徑或原始敏感內容。

### R8 — 驗收 fixture 必須守住 parser、correlation 與 write boundary

真實 Monitor parser/correlation/link fixtures MUST 覆蓋：

- 合格 Todo；issue mismatch；work_item mismatch。
- Tasks 缺失、重複標題、空、placeholder、重複 ID、未完成 task 為零。
- repo 內不存在 path、repo escape、symlink escape/traversal、非 scanner path。
- 已存在的 spec/design/plan scanner paths 仍能 link。
- path write 前所有拒絕案例 override bytes 不變。
- 已 linked 但 Todo metadata 不合格時 qualified_todos 為零；合格 source 的 path/revision/issue/work_item/Tasks result 穩定回傳。

## 明確排除

- 不實作 #1054 的 Manager first-Builder dispatch gate、typed zero/multiple diagnostics 或 Builder side-effect admission。
- 不實作 #1064/#1065 的 Monitor generation、override-input watermark、freshness 或 strict trusted snapshot reader；本 result 不代表 snapshot fresh。
- 不做 claim reconciliation、existing Candidate/PR recovery（#1055）、ship/backstop、merge、deployment 或 live run/snapshot 操作。
- 不要求 GitHub author identity/signature attestation，也不把 spec/design/plan 算作 Todo。
