### Fixed
- **Issue #315：retry-verify 重置時失效舊 exited verification job**：
  reviewer sandbox 依設計已清除、舊 job terminal 證據不可重驗，維持 exited
  會讓 dispatch 先 terminalize 舊 job 而永遠卡在
  `workflow input snapshot file missing`。`_manager_reset_workflow_for_retry_verify`
  於 CAS／admission 通過後將本 run 的 exited verify-phase job 標記 failed，
  explicit resume 隨即走 replacement dispatch；build phase job 與 active job
  不受影響。
