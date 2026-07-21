---
status: accepted
work_item: porcelain-request
---

# porcelain-request Specification

porcelain 計畫（epic #84）B2：`cortex request` 家族——mutation request 的顯性追蹤面。範圍以 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6.1 與 §5 為準。

## Requirements

### request 家族命令

`cortex request` SHALL 提供四個唯讀子命令：`list [--recent N]`（最近 requests，pending 與 done 合併依時間排序）、`show <request_id>`（型別、參數摘要、建立/完成時間、result/error）、`wait <request_id> [--timeout N]`（輪詢至 terminal：成功 exit 0、terminal failure exit 1、timeout exit 3）、`logs <request_id>`（best-effort 聚合該 request 的 done payload 與關聯 job 資訊）。

### 資料來源與唯讀性

全家族 MUST 只讀 control root（`requests/`、`done/`、`status.json`）與 job registry 檔案，MUST NOT 寫入任何狀態、MUST NOT 經 control queue 提交請求；daemon degraded 時 MUST 仍可運作。**不提供 `request submit`**（提交一律走語意化命令，見 UX 規格 §6.1 設計決策）。

### 輸出契約

預設人類可讀摘要；全子命令支援 `--json`（頂層含 `"schema": "cortex-porcelain/request/v1"`，snake_case，UTC ISO-8601）。exit code 遵循 UX 規格 §3（0/1/2/3）。

### 限制

以 B1 註冊表登記（`_FAMILY_MODULES` 加入 request 模組）；stdlib-only；TDD（tmp control root fixtures）；`test_zero_dependency_runtime` 續綠。
