### Fixed

- **Issue #158：`openspec archive` 產出規格 Purpose 初始化缺失**：`openspec archive` 現在在 archive 產生 `openspec/specs/*/spec.md` 後，會用對應 change proposal 的 `## Goals`（或備援欄位）填補 `## Purpose`，不再保留 `TBD` 預設文字。
