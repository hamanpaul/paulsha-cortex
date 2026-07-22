---
status: accepted
work_item: porcelain-request
---

# porcelain-request Design

## Goals

把「request 已受理但 CLI 5 秒 timeout 看不到結果」的 P0 摩擦，轉為可 list/show/wait/logs 的顯性追蹤面；為 B6 `run` 家族的 `--wait` 奠定共用輪詢基元。

## Decisions

### 模組與登記

`paulsha_cortex/porcelain/request.py`：模組層 `register(PorcelainCommand("request", ...))`；`__init__.py` 的 `_FAMILY_MODULES` 加入 `"paulsha_cortex.porcelain.request"`。子命令以 argparse subparsers 實作。

### 資料讀取

路徑一律取自 `paulsha_cortex.control.constants`（`requests_dir()`、`done_dir()`、`status_path()`）——不硬編路徑、不繞 PSC_* 契約。request 檔名即 request_id（`<id>.json`）；`list` 合併兩目錄、以檔案 mtime 排序、標注 `pending`/`done`；`show` 優先讀 done payload（含 `error`/`result`/`finished_at`），pending 時讀 requests payload。

### wait 輪詢

`wait` 以 0.5s 間隔輪詢 done 檔至 timeout（預設 120s）；terminal 判定：done payload `error` 非 null → exit 1，否則 exit 0；timeout → exit 3 並印追蹤提示。不依賴 daemon 存活。

### logs 聚合

`logs` = done payload 全文 + 依 result 內 `job_id`/`run_id` 欄位（存在時）從 job registry（`~/.agents/coordinator/jobs.json`）帶出對應 job 的 status/exit_code/log_path。找不到關聯時如實輸出「無關聯 job」。

### 測試策略

`tests/test_porcelain_request.py`：以 tmp dir monkeypatch control root（環境變數 PSC_CONTROL_ROOT）建構 requests/done fixtures；覆蓋 list 排序與標注、show pending/done 雙態、wait 三種退出碼（含 timeout 短 timeout 實測）、logs 關聯與無關聯、`--json` schema 欄位、degraded（無 status.json）仍可用。
