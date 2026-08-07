### Changed
- **封存批次 W2 三個已交付的 OpenSpec changes**：`2026-08-04-feat-slice-executor-model`
  （#294）、`2026-08-04-fix-preflight-closeout-order`（#263）、
  `2026-08-04-feat-task-type-combo-selector`（#202）已隨 PR #333／#336／#335 合併，
  但因本批改由人工管線收尾、未經 cortex ship 階段，change 目錄仍留在 active。
  以官方 archive 將 spec delta 折入 canonical specs，避免累積成 `validate --all`
  的長期阻塞（比照先前 14 個 7/25–26 遺留 active changes 的清理）。
