### Fixed

- **#716 canary 探針卡的唯讀模板**：worktree-isolation 是 `BUILDER_WRITE_FORBIDDEN` 卡，launcher 會替它選唯讀工作區模板 `cortex-job-ro[-jit]@`。但 driver 的 spec 檢查只接受可寫的 `cortex-job[-jit]@`，所以判定 `job spec authority mismatch`。現在改為要求唯讀模板，並在失敗訊息中列出不成立的條件名稱（#716）。
