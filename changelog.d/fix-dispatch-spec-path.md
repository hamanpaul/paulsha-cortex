### Fixed

- **Issue #98：修正 dispatch spec root 推斷**：`_infer_repo_root()` 在 `PSC_REPO_ROOT` 設定且 spec 路徑位於 `paths.repo_root()` 之外時，改回傳 configured repo root，避免沿外部路徑 `.git` 向上掃描導致錯誤的 repo 判斷。
