# Trust Root AGY builder 契約

- 新增 builder-principal 的 AGY toolchain／credential grant 與主體隔離的匯入 adapter。
- 以共用 compatibility preflight 在 Trust Root hardened runner 的 dispatch 前檢查
  launcher、toolchain、credential 三層，保留 direct mode 的 operator overlay 相容性，
  並補齊 generator、install plan、doctor、runbook 與回歸測試。
