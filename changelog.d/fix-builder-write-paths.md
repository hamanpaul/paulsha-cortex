### Fixed

- **Issue #118：跨 repo 派工的 builder 可寫入範圍修正**：`paulsha_cortex/persona/personas.yaml` 將 `builder` 的 `write_paths` 由 `paulsha_cortex/**` 調整為 `"**"`，避免非本體 repo 派工時 write-path 被誤拒。`changelog.d` 與 `CHANGELOG.md` 也同步更新。
