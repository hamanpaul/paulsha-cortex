---
status: accepted
work_item: add-cortex-version-flag
---

# add-cortex-version-flag Design

## Decisions

### 實作位置

在 `paulsha_cortex/cli.py` 的 `main()` 子命令路由之前處理 `--version`（比照既有 `-h/--help` 的頂層特殊處理），避免落入 coordinator 透傳路徑的必填 `cmd` 檢查。

### 版本解析

以 `importlib.metadata.version("paulsha-cortex")` 為權威來源；`PackageNotFoundError` 時 fallback 輸出 `0.0.0+unknown`。不直接讀 repo `VERSION` 檔（安裝環境不存在該檔）。

### 輸出格式

單行 `cortex <version>`，stdout，exit 0。不加多餘裝飾，便於腳本解析。

### 測試策略

`tests/test_cli_version.py`：以 subprocess 或直接呼叫 `main(["--version"])` 斷言輸出含套件版本且 exit 0；先 RED（現況會落入 argparse 必填 `cmd` 錯誤）後實作轉 GREEN。
