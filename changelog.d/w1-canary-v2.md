### Changed
- **W1 canary 重識別為 fix-persona-catalog-portability-v2（#295／#291）**：三代 run
  因基礎設施缺陷（#299 reclaim 短路、#302 載入唯一性、#303 gate 測試隔離洩漏）先後
  superseded，觸發 #218 語意 re-claim 世代熔斷（SEMANTIC_RECLAIM_LIMIT=3）。依既有
  「-v2 識別」慣例改 frontmatter `work_item` 與 `.cortex/work-items.yaml`（檔案路徑
  不動），取得全新 claim identity 續作。
