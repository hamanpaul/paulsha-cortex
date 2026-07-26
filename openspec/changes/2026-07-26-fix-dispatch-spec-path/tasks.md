---
status: accepted
work_item: fix-dispatch-spec-path
---

# Tasks

- [ ] [RED] `tests/test_fix_dispatch_spec_path.py`：spec 位於 `~/.agents/specs/`（repo 外）、`PSC_REPO_ROOT` 已設定時，`_infer_repo_root(spec_path)` 回傳 `paths.repo_root()` 而非 `spec_path.parent` 或沿 `.git` 搜尋結果。
- [ ] [RED] spec 位於 repo 內時行為不變（回傳 repo root via `.git` walk）。
- [ ] [RED] `_infer_repo_root` 在 `PSC_REPO_ROOT` 未設定且 spec 在 repo 外時仍維持既有 fallback 行為（向後相容）。
- [ ] [實作] `paulsha_cortex/coordinator/autonomy.py`：`_infer_repo_root()` 優先檢查 spec 是否在 `paths.repo_root()` 子樹下；若不在，回傳 `paths.repo_root()`。
- [ ] [同步與驗證] `changelog.d/fix-dispatch-spec-path.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#98` 條目。
- [ ] [同步與驗證] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] [同步與驗證] 勾選本 tasks.md 對應項並以 conventional commit 提交。