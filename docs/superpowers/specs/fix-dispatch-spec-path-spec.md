---
status: accepted
work_item: fix-dispatch-spec-path
---

# fix-dispatch-spec-path Specification

`#98`：修正 `autonomy._infer_repo_root()` 在 spec 位於 repo 外時未回退至 `PSC_REPO_ROOT`，導致 repo-relative plan 路徑解析錯誤、dispatch pinning 失敗。

## Requirements

### R1 spec 在 repo 外時回傳 PSC_REPO_ROOT

`paulsha_cortex/coordinator/autonomy.py::_infer_repo_root(spec_path)` 當 `spec_path` 不在 `paths.repo_root()` 子樹下時，MUST 回傳 `paths.repo_root()`，因為 repo-relative contract path 應以設定 repo root 為基準。MUST NOT 回傳 `spec_path.parent` 或沿 `.git` 搜尋結果。

### R2 spec 在 repo 內行為不變

spec 位於 `paths.repo_root()` 子樹下時，`_infer_repo_root` MUST 維持既有 `.git` parent walk 行為。

### R3 限制

- stdlib-only；TDD（mock/monkeypatch `paths.repo_root` 驗證回傳值）。
- 不得改變既有對外 CLI envelope schema。
- `test_zero_dependency_runtime` 續綠；`python3 -m policy_check --repo .` 0 fail。