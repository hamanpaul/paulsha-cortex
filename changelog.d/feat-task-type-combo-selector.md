### Added
- **Issue #202：task_type combo selector 與 fix-standard workflow**：
  依 durable snapshot 的 issue title 與 deck taxonomy 自動選牌，讓 `feat` 對應
  `feature-oneshot`、`fix` 對應 `fix-standard`；`unknown_type`／`ambiguous` fail-closed，
  `absent`／`unparseable`／combo 缺口則留下 `combo_selection` provenance 並沿用預設 combo。
  `cortex work start`／`cortex run work start` 新增 `--combo` authoritative override，
  `cortex stat --combo-selections` 可彙總 source × task_type。

### Fixed
- **Issue #202 補遺：durable snapshot 不可用時 combo 選擇改走 fail-soft**：
  `claim.mapped_issue_titles` 先前只在 snapshot hash mismatch 時 bypass（回傳 `None`）；
  `_load_snapshot` 因 snapshot 不存在／不可讀／schema 損壞而 raise 的 `ValueError`
  （含其子類別 `AuthorityValidationError`）未被攔截，會一路炸穿到
  `work_bridge.start_canonical_workflow`，讓沒有現成 durable snapshot 的 claim 直接
  拋例外而非落回 `feature-oneshot` 預設 combo。`mapped_issue_titles` 現在對這些情況
  也回傳 `None`，與既有 hash-mismatch bypass 走同一條路徑；`load_work_authorities`／
  `load_work_authority` 仍維持 fail-hard（沒有安全的 WorkAuthority 預設值可退）。
