# #1363 autosync 後恢復既有 PR 的 retry-build

- delivery journal 以 exact run／claim row 記錄 Manager push、但沒有 legacy `delivery_binding` 欄位時，Builder admission 可由唯一 `WorkflowRun.pr_refs` 取得 PR 編號；仍驗證 run／claim identity、push ancestry、目前 open PR、issue 狀態與 Todo mapping。
