---
status: accepted
work_item: qualification-test-fixed-clock
---

# test_qualification_lifecycle_842 不依賴牆鐘

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1340`。
- 現況：`tests/test_qualification_lifecycle_842.py` 的 `test_q04_…acl_separation` 讀真實牆鐘，偶發 `reviewed_at in the future`。單獨重跑 3 次都通過。PR 建立後只要 gate 失敗，就會退化成整輪重建，所以這個 flaky 的成本很高。
- 只改測試與必要的時鐘注入點，不改 qualification 的判準。

## Tasks

- [x] **T1 固定時鐘**：測試改為注入固定的 `now`（或可控的 clock），以固定值驗證 `reviewed_at` 的上下界。
- [x] **T2 掃描同類**：同一檔案及相關 helper 中其他依賴牆鐘的比較，一併改為注入。
- [x] **T3 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
