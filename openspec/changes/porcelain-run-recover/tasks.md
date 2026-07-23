---
status: accepted
work_item: porcelain-run-recover
---

# Tasks

- [x] 1.1 RED：`tests/test_porcelain_run.py`（映射正確性、退出碼、request_id、`--json`）。
- [x] 1.2 RED：`tests/test_porcelain_recover.py`（映射正確性、`--actor` 必填、退出碼、`--json`、無危險旁路旗標）。
- [ ] 1.3 `paulsha_cortex/porcelain/run.py` 與 `recover.py` 實作 + `_FAMILY_MODULES` 登記。
- [ ] 1.4 README CLI 段落補 `run`／`recover`（R-16）；`changelog.d/porcelain-run-recover.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 porcelain-run-recover 字樣）。
- [ ] 1.5 pytest 全綠、policy_check 0 fail、`git diff --check` 乾淨。
