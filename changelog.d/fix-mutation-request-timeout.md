### Fixed

- **Issue #152：mutation request 分級 timeout 與 pending 回報**：`_submit_mutation_request` 依 request 類型套用分級 timeout（`fanout`/`tick` 60 秒、`complete`/`work`/`work-action`/`run` 30 秒、其他 5 秒），`poll` 超時時保留成功派工的可追蹤結果（含 `req_id`），回傳 `EXIT_SUBMITTED_PENDING`（3）避免將成功派工誤判為失敗。
