### Fixed
- **#623:** doctor 的 service-path discovery 現在接受 generated trust-root
  manager/monitor units 所宣告的受保護 deploy `EnvironmentFile`，維持 repo/runtime
  identity 可見，並對缺失、分歧或不可驗證的安裝狀態維持 fail-closed。
