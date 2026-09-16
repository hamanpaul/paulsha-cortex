---
type: change
scope: openspec
---
將 `openspec/changes/installer-shared-config-guard/`（僅含 tasks.md，PR 913 交付時未 archive）
移至 `openspec/changes/archive/2026-09-16-installer-shared-config-guard/`。該 change 沒有
proposal.md，`openspec archive` 不認得它，故以 git mv 手動歸檔。歸檔後 cortex daemon 不再把它
當 active change 掃成 `not_claimable: missing_issue`。
