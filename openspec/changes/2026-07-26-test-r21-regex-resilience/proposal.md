---
status: accepted
work_item: test-r21-regex-resilience
---

## Goals

修正 `tests/test_onboarding_docs_contract.py` 的兩個 regex 漏洞：`BASH_FENCE_RE` 僅匹配 LF 不匹配 CRLF、`PERSONAL_ABSOLUTE_PATH_RE` 不涵蓋 Windows 路徑。

## Why

`#169` 回報：
1. `BASH_FENCE_RE = re.compile(r"```bash\n(.*?)```")` 僅匹配 `\n`（LF），不匹配 `\r\n`（CRLF）或 fence marker 後有 trailing whitespace → CRLF 的 bash code block 被靜默跳過（false assurance）。
2. `PERSONAL_ABSOLUTE_PATH_RE` 僅涵蓋 `/home` 和 `/Users`，不涵蓋 Windows `C:\Users\...` 路徑。

## What Changes

- `tests/test_onboarding_docs_contract.py`：
  - `BASH_FENCE_RE` 改為容忍 `\r?\n` 與 fence marker 後 trailing whitespace。
  - `PERSONAL_ABSOLUTE_PATH_RE` 新增 Windows drive-letter path pattern。
  - 新增 CRLF bash fence 與 Windows absolute path 測試案例。

## Capabilities

### Modified Capabilities

- `onboarding-docs-contract`：R-21 regex 涵蓋 CRLF bash fence 與 Windows absolute path，消除 false assurance。