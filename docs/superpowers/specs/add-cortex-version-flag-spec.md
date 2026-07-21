---
status: accepted
work_item: add-cortex-version-flag
---

# add-cortex-version-flag Specification

porcelain 計畫（epic #84）的 dogfood canary：以最小真實變更驗證 coordinator 全自動交付鏈，同時補上 release 工程需要的版本可見性。

## Requirements

### 頂層 --version 旗標

`cortex --version` SHALL 輸出目前安裝的套件版本字串並以 exit code 0 結束，不得要求任何子命令。

版本 SHALL 優先取自已安裝套件 metadata（`importlib.metadata`）；metadata 不可得時 SHALL fallback 至可辨識的開發版本字串。

### 範圍限制

本變更 SHALL 僅新增頂層旗標，不得改動任何既有子命令的行為、參數或輸出。

本變更 SHALL 維持 stdlib-only，不新增 runtime 依賴。

### 驗證

變更 SHALL 附帶單元測試（先 RED 後 GREEN），並保持既有測試全綠。
