---
status: accepted
work_item: porcelain-request
---

# Design

## Decisions

- **唯讀不提交**：request 家族只讀 `control.constants` 契約路徑（requests/done/status.json）與 job registry；提交語意留給 B6 `run` 家族，避免 generic submit 繞過語意驗證。
- **wait 不依賴 daemon**：輪詢 done 檔而非 control 回應——daemon degraded 時仍可判 pending/timeout（exit 3）。
- **logs best-effort**：done payload 為主、job registry 關聯為輔，查無關聯如實說明，不合成資料。
- **`--json` 版本化**：`cortex-porcelain/request/v1`，additive-only 演進，比照 repo 既有 schema 慣例。
