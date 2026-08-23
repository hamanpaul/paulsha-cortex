### Added
- **#623（RED）：** 新增 trust-root manager visibility regression，重現 generated
  manager/monitor units 指向受保護 deploy `EnvironmentFile` 時，Manager 的
  service-path discovery 仍無法讀出宣告的 repo/runtime identity。
