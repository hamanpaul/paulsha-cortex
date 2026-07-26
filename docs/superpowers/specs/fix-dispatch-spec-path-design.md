---
status: accepted
work_item: fix-dispatch-spec-path
---

# fix-dispatch-spec-path Design

## Decisions

### D1 優先判斷 spec 是否在 repo 子樹

`_infer_repo_root(spec_path)` 先 resolve `spec_path` 與 `paths.repo_root()`，判斷前者是否為後者子目錄。若是 → 既有 `.git` walk。若否 → 回傳 `paths.repo_root()`。理由：repo-relative contract path（如 `plan`）的解析基準應為設定 repo root，不是 spec 檔所在目錄。

### D2 向後相容

`PSC_REPO_ROOT` 未設定時 `paths.repo_root()` 回退到 cwd 解析，行為與既有一致。不改動 `paths.repo_root()` 本身。

### 風險與 mitigation

- 測試以 `tmp_path` 建構 repo 外 spec + monkeypatch `paths.repo_root` 回另一目錄，驗證回傳值為 monkeypatch 的 root。
- 既有 repo 內 spec 路徑不受影響——新增分支判斷在「不在子樹」時才生效。