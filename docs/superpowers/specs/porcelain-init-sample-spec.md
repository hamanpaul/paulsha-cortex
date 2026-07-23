---
status: accepted
work_item: porcelain-init-sample
---

# porcelain-init-sample Specification

porcelain 計畫（epic #84）B7：`cortex init-sample`——第一個可成功的 sample workflow（issue #93）。範圍以 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6.7 為準。

## Requirements

### init-sample 命令

`cortex init-sample --task "<描述>" [--combo COMBO] [--change NAME]` SHALL 包裝既有 `deck compile --emit`；`--combo` 省略時預設 `feature-oneshot`，僅接受既有 deck combo 白名單，未知值 MUST 以 exit 2 結束。

### hold 強制與輸出內容

產出的 spec MUST 一律 `dispatch: hold`；輸出 MUST 包含 spec 檔案路徑、必補欄位清單（`plan` 需改為確切路徑、`target_branch` 需補 `main`、`verification` 需補完整物件含 persona-scope + name=policy command + full_suite baseline）、`deck verify` 檢核命令，以及「如何手動翻 auto」的說明文字。

### 安全邊界

本命令 MUST NOT 自動將 spec 的 `dispatch` 改為 `auto`；不得提供一鍵翻 auto 的旁路旗標。使用者 MUST NOT 需要事先理解 deck/spec 概念即可完成本命令並得到可讀的下一步指引。

### 輸出契約

預設人類可讀摘要；SHALL 支援 `--json`（頂層含 `"schema": "cortex-porcelain/init-sample/v1"`）。exit code 遵循 UX 規格 §3。

### 限制

以 B1 註冊表登記；stdlib-only；TDD（mock `deck compile --emit`）；`test_zero_dependency_runtime` 續綠。
