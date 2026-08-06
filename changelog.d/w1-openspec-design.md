### Fixed
- **批次 W1 openspec design.md 補件（#295／#291、#260、#178、#139）**：`_artifact_rows`
  的 design kind 來源是 `openspec/changes/<change>/design.md`；缺檔時
  `assess_planning_completeness` 永遠 incomplete，claim 後 define 必然繞進 heterogeneous
  brainstorm 並靜默 needs_human（7/30 批次全卡 define 的根因）。為四個 work item 補上
  design.md（決策摘要＋指回 superpowers design 全文），使 define 走 planning-complete
  deterministic 路徑。
