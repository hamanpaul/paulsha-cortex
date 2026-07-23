---
status: accepted
work_item: porcelain-init-sample
---

## Goals

讓新手不需要事先理解 deck/spec 概念，也能產出第一個可成功執行的 sample workflow，並清楚知道下一步（issue #93）。

## Why

`deck compile --emit` 產出的 frontmatter 骨架（`target_branch: null`、`verification: null`、`plan` 為 glob）與 auto 派工實際要求的合法形狀落差極大，dogfood F4 證實此落差只能從測試程式碼逆推、未文件化。B7 在既有 `deck compile --emit` 之上疊加必補欄位清單與檢核指引，並以 `dispatch: hold` 保底，避免新手誤觸自動派工。

## What Changes

- 新增 `paulsha_cortex/porcelain/init_sample.py`：`cortex init-sample`（包裝 `deck compile --emit`、強制 hold、必補欄位清單、`deck verify` 提示、`--json`）。
- `porcelain._FAMILY_MODULES` 登記 init-sample 模組。
- README 命令面補 init-sample 段（R-16）。
- 刻意**不提供**一鍵翻 auto 的旁路旗標。

## Capabilities

### New Capabilities

- `porcelain-init-sample`: 新手第一個 sample workflow 的導引式產出契約——包裝 `deck compile --emit`、強制 hold、必補欄位清單與翻 auto 說明。
