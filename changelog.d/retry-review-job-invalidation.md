### Fixed
- **Issue #315 補遺：retry-review 重置時同步失效舊 exited review job**：比照
  retry-verify，reviewer sandbox 已清的舊 review job 標記 failed，explicit
  resume 走 replacement dispatch；verify／build job 不受影響。
