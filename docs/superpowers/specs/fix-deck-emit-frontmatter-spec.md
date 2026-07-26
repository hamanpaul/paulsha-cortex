---
status: accepted
work_item: fix-deck-emit-frontmatter
---

# fix-deck-emit-frontmatter Specification

`#101`：修正 `deck compile --emit` 硬編碼 `target_branch: null` 與 `verification: null`，使 emit 產生的 frontmatter 符合 auto dispatch contract，並補文件。

## Requirements

### R1 target_branch 非空

`deck compile --emit` 產生的 frontmatter `target_branch` MUST 非空，從 combo/work-item context 推導。MUST NOT 硬編碼 `null`。context 不足時 MUST 提供 fallback default 並 emit warning。

### R2 verification 物件完整

`verification` MUST 為含以下結構的物件：
- `docs_class`：文件類別
- `checks`：含 persona-scope、`name=policy` command、`tests`、`full_suite`（含 `baseline: no-regression`）

MUST NOT 硬編碼 `null`。

### R3 文件化 auto dispatch contract

`docs/` 或 README MUST 記錄 auto dispatch contract 的 verification 物件結構、必填欄位、範例。使使用者不需讀測試碼即可理解。

### R4 限制

- stdlib-only；TDD。
- 不得改變既有對外 CLI envelope schema 字串。
- `test_zero_dependency_runtime` 續綠；`python3 -m policy_check --repo .` 0 fail。