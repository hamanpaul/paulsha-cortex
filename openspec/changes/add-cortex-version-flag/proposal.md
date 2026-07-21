---
work_item: add-cortex-version-flag
---

## Why

porcelain 計畫（epic #84、issue #86）需要一個最小真實批次做 dogfood canary，在賭上七家族之前驗證 deck → tick → copilot build → ForeignReview → 自動 push/PR/bot review/merge → archive 的全自動交付鏈。同時，deep research 指出 cortex 缺乏版本可見性（對照 `--version` 慣例），是 release 工程的前置需求。

## What Changes

- `paulsha_cortex/cli.py` 新增頂層 `--version` 旗標：輸出安裝套件版本（`importlib.metadata`，fallback `0.0.0+unknown`），exit 0。
- 新增 `tests/test_cli_version.py`（TDD：先 RED 後 GREEN）。
- README CLI 段落與 changelog fragment 同步。
- 不改動任何既有子命令行為；不新增 runtime 依賴。

## Capabilities

### New Capabilities

- `cli-version-reporting`: cortex CLI 的版本可見性——頂層 `--version` 輸出安裝版本，供人類與腳本辨識運行中版本。
