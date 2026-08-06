---
status: accepted
work_item: fix-deck-emit-frontmatter
---

## Goals

修正 `deck compile --emit` 產生的 frontmatter 硬編碼 `target_branch: null` 與 `verification: null`，使其符合 auto dispatch contract（非空 `target_branch` + 完整 `verification` 物件），並補文件記錄 auto dispatch contract 的 verification 物件結構。

## Why

`#101` 回報：`paulsha_cortex/deck/compile.py` 第 227–228 行硬編碼 `target_branch: null` 與 `verification: null`。auto dispatch contract 要求精確 plan path、非空 `target_branch`、完整 `verification` 物件（含 `docs_class` + `checks`（persona-scope + `name=policy` command + tests + full_suite with `baseline: no-regression`））。目前僅能從測試碼推斷契約，無使用者文件。

## What Changes

- `paulsha_cortex/deck/compile.py`：`--emit` 產生的 frontmatter 從 combo/work-item context 推導 `target_branch`，並填入 `verification` skeleton 符合 auto dispatch contract。
- `docs/`：記錄 auto dispatch contract（verification 物件結構、必填欄位）。

## Capabilities

### Modified Capabilities

- `deck-compile`：`--emit` 產生符合 auto dispatch contract 的 frontmatter（target_branch + verification skeleton）。
- `dispatch-contract`：文件化 auto dispatch contract 的 verification 物件結構與必填欄位。