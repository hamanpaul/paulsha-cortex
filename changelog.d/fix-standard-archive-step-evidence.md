- **#885 fix-standard-archive-step-evidence**：fix-standard 現在可用唯一 Manager `openspec-archive`
  job 加 Git ancestry 判定 archive-applied；`retry-build` 會按 exact Candidate Git tree 偵測
  active/archive 並存並回報 warning，ship/local-closeout 對 `openspec archive` Aborted、未搬移
  active change 與 post-archive 並存一律 fail-closed。
