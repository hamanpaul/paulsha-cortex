### Fixed

- **builder workflow identity 先過濾 build capability**：`_select_workflow_identity()` 現在會先排除不具 `build` 能力的 builder 候選，再套用 `primary_domain` 偏好，避免 google primary domain 把 build 卡誤派給僅支援 planning 的身分而陷入 malformed 重派。
