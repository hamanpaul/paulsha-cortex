# 752-state-path-wiring

- **`#752` 補遺：retry-card 的 evidence 寫入接 `resolved_state_path`。** dispatcher
  的原始 `state_path` 參數在 production（daemon 請求路徑）恆為 None，帶 reason 的
  retry-card 因此一律被前置檢查拒於「requires a durable state path」——人裁通道
  上線即不可用。改接與其餘 action 相同的 `resolved_state_path`。
