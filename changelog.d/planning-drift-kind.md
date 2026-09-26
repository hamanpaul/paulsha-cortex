修正 #551、#562：planning baseline 取樣改為前後快照一致才採用、不穩定時重試一次，並忽略 uid/gid 與 xattr 差異；持續變動仍保留 fail-closed。operator worktree drift 改以結構化 `failure_kind` 傳遞、分類並供 `recover-planning` 讀取，不再依賴錯誤訊息文字。
