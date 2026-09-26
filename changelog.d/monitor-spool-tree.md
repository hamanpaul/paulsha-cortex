#585、#567：Monitor 每輪只掃一次共享 spool，驗證 producer 的 repo 形狀，並在隔離 30 天後清理 quarantine；改由 canonical checkout 的本機 `git ls-tree -r -t -z` 讀取 default-branch tree，shallow ancestry 無法判定時提供明確診斷且不自動 unshallow。
