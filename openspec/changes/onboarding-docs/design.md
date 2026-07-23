---
status: accepted
work_item: onboarding-docs
---

# Design

## Decisions

- **內容來源治理**：每份文件動筆前列引用來源清單，逐句可回溯，不杜撰未落地行為。
- **名詞單一來源**：Concepts 文件沿用 UX 規格 §9 定義，不另造詞彙。
- **診斷步驟依賴排序**：Troubleshooting／Runbook 引用 B3/B4 的殭屍偵測輸出，未落地時以既有 `doctor`／`systemctl status` 過渡並標註。
- **路徑衛生**：全文採 `$HOME`／`~`／相對路徑，符合 R-21。
