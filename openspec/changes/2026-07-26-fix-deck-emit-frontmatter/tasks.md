---
status: accepted
work_item: fix-deck-emit-frontmatter
---

# Tasks

- [ ] [RED] `tests/test_fix_deck_emit_frontmatter.py`：`deck compile --emit` 產生的 frontmatter `target_branch` 非空（從 combo/work-item context 推導），`verification` 為含 `docs_class`、`checks`（含 persona-scope + `name=policy` command + `tests` + `full_suite` with `baseline: no-regression`）的完整物件。
- [ ] [RED] 既有 `target_branch: null` 與 `verification: null` 的 emit 結果不再出現。
- [ ] [實作] `paulsha_cortex/deck/compile.py`：`--emit` render frontmatter 時從 combo/work-item context 推導 `target_branch`，填入 verification skeleton。
- [ ] [實作] docs/ 或 README：記錄 auto dispatch contract（verification 物件結構、必填欄位、範例）。
- [ ] [同步與驗證] `changelog.d/fix-deck-emit-frontmatter.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#101` 條目。
- [ ] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。