### Added
- **Issue #202：task_type combo selector 與 fix-standard workflow**：
  依 durable snapshot 的 issue title 與 deck taxonomy 自動選牌，讓 `feat` 對應
  `feature-oneshot`、`fix` 對應 `fix-standard`；`unknown_type`／`ambiguous` fail-closed，
  `absent`／`unparseable`／combo 缺口則留下 `combo_selection` provenance 並沿用預設 combo。
  `cortex work start`／`cortex run work start` 新增 `--combo` authoritative override，
  `cortex stat --combo-selections` 可彙總 source × task_type。
