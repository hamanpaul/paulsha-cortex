---
status: accepted
work_item: fix-deck-emit-frontmatter
---

# fix-deck-emit-frontmatter Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_deck_emit_frontmatter.py`：
  - `deck compile --emit` 產生 frontmatter `target_branch` 非空（從 context 推導）。
  - `verification` 為完整物件含 `docs_class`、`checks`（persona-scope + policy command + tests + full_suite with baseline: no-regression）。
  - 不再出現 `target_branch: null` 或 `verification: null`。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/deck/compile.py`：`--emit` render 時從 context 推導 `target_branch`，填入 verification skeleton。
- [ ] `docs/`：新增 auto dispatch contract 文件（verification 物件結構、必填欄位、範例）。

### 3. 同步與驗證

- [ ] `changelog.d/fix-deck-emit-frontmatter.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#101` 條目。
- [ ] README 對應段同步連結（R-18）。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-fix-deck-emit-frontmatter/tasks.md` 對應項並以 conventional commit 提交。