---
status: accepted
work_item: test-r21-regex-resilience
---

# test-r21-regex-resilience Specification

`#169`：修正 `test_onboarding_docs_contract.py` 的 regex 漏洞——CRLF bash fence 與 Windows absolute path 未被涵蓋。

## Requirements

### R1 BASH_FENCE_RE 容忍 CRLF + whitespace

`BASH_FENCE_RE` MUST 匹配：
- LF（`\n`）分隔的 bash fence（既有行為）。
- CRLF（`\r\n`）分隔的 bash fence。
- fence marker 後有 trailing whitespace（如 ` ```bash `）的 code block。

MUST NOT 靜默跳過上述任一情況。

### R2 PERSONAL_ABSOLUTE_PATH_RE 涵蓋 Windows

`PERSONAL_ABSOLUTE_PATH_RE` MUST 涵蓋：
- `/home/...` 與 `/Users/...`（既有）。
- Windows drive-letter path 如 `C:\Users\...`。

### R3 限制

- test-only；不改 production code。
- 既有測試全綠（無 regression）。
- `python3 -m policy_check --repo .` 0 fail。