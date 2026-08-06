### Fixed
- **封存 14 個 7/25–26 遺留 active OpenSpec changes**：對應功能皆已 merge（直接
  PR 交付、runs closure 停滯），但 change 目錄缺 specs delta，
  `openspec validate --all` 14 項 fail 使 ship 的 PR-metadata preflight（openspec
  gate）擋下所有新交付。以官方 `openspec archive` 逐一封存，validate --all 回到
  0 failed。
