# Fixed

- abandon 的 build commit 改存於 `refs/archive/<work>-<sha8>`，不再污染發布 tag 查找；job clone 僅取得 `v*` tag。trust-root 仍接受既有 `refs/tags/archive/*`，無需遷移舊來源樹。
