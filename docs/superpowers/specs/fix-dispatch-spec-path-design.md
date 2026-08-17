---
status: accepted
work_item: fix-dispatch-spec-path
---

# fix-dispatch-spec-path Design

## Decisions

### D1 優先判斷 spec 是否在 repo 子樹

`_infer_repo_root(spec_path)` 先 resolve `spec_path` 與 `paths.repo_root()`，判斷前者是否為後者子目錄。若是 → 既有 `.git` walk。若否 → 回傳 `paths.repo_root()`。理由：repo-relative contract path（如 `plan`）的解析基準應為設定 repo root，不是 spec 檔所在目錄。

### D2 向後相容

~~`PSC_REPO_ROOT` 未設定時 `paths.repo_root()` 回退到 cwd 解析，行為與既有一致。不改動 `paths.repo_root()` 本身。~~

> **issue #612 推翻**：這條「向後相容」正是後來的事故面。cwd 就是 manager daemon 的
> `WorkingDirectory`＝ operator 的真實 checkout，於是相對 spec 路徑會讓 production
> 動作（`git fetch`／`rev-parse`／`merge-base`／`worktree remove`）落在錯的 repo 上並
> 「成功」。現行契約：`paths.repo_root()` 未宣告即 `RepoRootUnresolvedError`；
> `_infer_repo_root` 只收絕對 spec 路徑，推不出 repo 根時帶 `DiagnosticReason`
> fail-closed。詳見 README 路徑契約段與 `tests/test_repo_root_fail_closed_612.py`。

### 風險與 mitigation

- 測試以 `tmp_path` 建構 repo 外 spec + monkeypatch `paths.repo_root` 回另一目錄，驗證回傳值為 monkeypatch 的 root。
- 既有 repo 內 spec 路徑不受影響——新增分支判斷在「不在子樹」時才生效。