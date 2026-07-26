---
status: accepted
work_item: fix-dispatch-spec-path
---

# fix-dispatch-spec-path Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_fix_dispatch_spec_path.py`：
  - spec 位於 repo 外（`tmp_path / "agents" / "specs" / "foo-spec.md"`）、`PSC_REPO_ROOT` 指向另一 `tmp_path` 時，`_infer_repo_root` 回傳 `paths.repo_root()`。
  - spec 位於 repo 內時行為不變。
  - `PSC_REPO_ROOT` 未設定 + spec 在 repo 外時維持既有 fallback。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/coordinator/autonomy.py`：`_infer_repo_root()` 新增子樹判斷——spec 不在 `paths.repo_root()` 子樹下時回傳 `paths.repo_root()`。

### 3. 同步與驗證

- [ ] `changelog.d/fix-dispatch-spec-path.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `#98` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-fix-dispatch-spec-path/tasks.md` 對應項並以 conventional commit 提交。