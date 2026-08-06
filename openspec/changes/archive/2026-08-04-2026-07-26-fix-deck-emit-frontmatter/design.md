---
status: accepted
work_item: fix-deck-emit-frontmatter
---

# fix-deck-emit-frontmatter Design

## Decisions

### D1 target_branch 從 context 推導

`deck compile --emit` render frontmatter 時，`target_branch` 從 combo/work-item context 推導（如 `feature/<slug>` 或 `wt/<feature>/<subtask>`），而非硬編碼 `null`。若 context 無分支資訊，emit 時 warning 並填入合理 default（如 `feature/<work-item-slug>`）。

### D2 verification skeleton 符合 auto contract

`verification` 物件包含：
- `docs_class`：文件類別
- `checks`：含 persona-scope（reviewer persona）、`name=policy` command（policy_check）、`tests`（pytest 指令）、`full_suite`（含 `baseline: no-regression`）

skeleton 值可從現有 dispatch contract 測試碼推斷的期望結構填入。

### D3 文件化 auto dispatch contract

在 `docs/` 新增文件記錄 auto dispatch contract：verification 物件的必填欄位、結構、範例 frontmatter。使使用者不需讀測試碼即可理解契約。

### 風險與 mitigation

- 推導 `target_branch` 時 context 可能不完整 → fallback 到 `feature/<slug>` 並 warning。
- verification skeleton 欄位需與實際 dispatch contract 保持同步 → 文件與測試同時維護。