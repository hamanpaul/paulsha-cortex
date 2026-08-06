### Changed
- **feat-work-gc 與 design-task-type-taxonomy 重識別為 -v2（#178／#139）**：兩者
  的三代 run 因基礎設施缺陷鏈（drift／ledger／schema 系列）superseded 觸發 #218
  世代熔斷。依「-v2 識別」慣例改檔名、workstream 目錄、frontmatter `work_item`
  與 `.cortex/work-items.yaml`，於修復齊備的 main 上以新 claim identity 重跑。
