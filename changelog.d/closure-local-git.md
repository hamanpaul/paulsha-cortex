修正 #567：delivery closure 在 canonical checkout fetch default branch 後，以本機 Git 驗證 merge ancestry、merge parents、OpenSpec tree 與 Todo blob／內容；checkout 缺失或 shallow 時 fail-closed 並提供診斷，不自動 unshallow。
