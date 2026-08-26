- 修正 RC container qualification 的 fresh-install rollback：unknown-state scanner 會依
  archived receipt inventory 辨識刻意保留的 checkout／content-addressed venv 與必要 parent，
  但仍對 foreign sibling fail closed；runtime scaffold fixture 延後至 rollback 與 clean reinstall
  之後，讓 qualification 真正驗證 installer transaction，而非混入 harness 外部狀態。
