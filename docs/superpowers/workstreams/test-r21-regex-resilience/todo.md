---
status: accepted
work_item: test-r21-regex-resilience
---

# test-r21-regex-resilience Todo

## Tasks

- [ ] 將 issue #169、active OpenSpec change `2026-07-26-test-r21-regex-resilience` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex 完成 #169 修復（TDD）：BASH_FENCE_RE 容忍 CRLF + whitespace、PERSONAL_ABSOLUTE_PATH_RE 涵蓋 Windows path。
- [ ] ForeignReview 通過；operator 對抗 review 核可。
- [ ] CRLF bash fence 與 Windows path 測試案例全綠（驗證 regex 修復）。