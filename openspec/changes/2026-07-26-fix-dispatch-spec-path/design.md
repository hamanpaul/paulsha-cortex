---
status: accepted
work_item: fix-dispatch-spec-path
---

# fix-dispatch-spec-path Design

## Decisions

### D1 spec 在 repo 外 → 回傳 PSC_REPO_ROOT

`_infer_repo_root(spec_path)` 新增邏輯：先判斷 `spec_path` 是否在 `paths.repo_root()` 子樹下（`Path.is_relative_to` 或 `resolve()` 比較）。若不在，直接回傳 `paths.repo_root()`，因為 repo-relative contract path（plan 等）應以設定的 repo root 為基準，而非 spec 所在目錄。

### D2 spec 在 repo 內 → 維持既有行為

spec 在 repo 子樹下時，沿用既有 `.git` parent walk 邏輯，不改變行為，確保向後相容。

### 風險與 mitigation

- `PSC_REPO_ROOT` 未設定時 `paths.repo_root()` 回退到 cwd 解析——此為既有行為，不受影響。
- 測試以 `tmp_path` 模擬 repo 外 spec + monkeypatch `paths.repo_root` 回另一 `tmp_path` 驗證回傳值。