---
status: accepted
work_item: onboarding-docs
---

# onboarding-docs Design

## Goals

把偏向專家維運者的既有 README 敘事，補齊為新手可依循的完整上手到日常維運文件集，並讓已知故障情境（F1/F8/F34 等）第一次有對照排除步驟。

## Decisions

- **內容來源治理**：每份文件動筆前先列出「引用來源」清單（UX 規格章節、issue 編號、dogfood F-finding 編號），撰寫時逐句可回溯，不得杜撰未落地的命令行為。
- **名詞單一來源**：Concepts 文件直接沿用 UX 規格 §9 的 spec→job→slice→work 定義文字，不重新發明或簡化到失真。
- **診斷步驟依賴排序**：Troubleshooting／Runbook 引用 B3 `inspect service` 與 B4 `service status` 的殭屍偵測輸出作為診斷步驟；若撰寫時 B3/B4 尚未落地，先以既有 `doctor`／`systemctl status` 過渡寫法呈現並於文中標註「B3/B4 落地後改用 `cortex inspect service`」。
- **路徑衛生落實**：所有範例指令一律使用 `$HOME`、`~`、或 `$(git rev-parse --show-toplevel)`，不得出現使用者名或機器識別字串，符合 R-21。
