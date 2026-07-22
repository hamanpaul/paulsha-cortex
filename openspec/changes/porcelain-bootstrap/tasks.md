---
status: accepted
work_item: porcelain-bootstrap
---

# Tasks

- [ ] 1.1 RED：`tests/test_porcelain_bootstrap.py`（preflight 缺失情境、`--dry-run`、正常流程串接、sample 降級、`--json`）。
- [ ] 1.2 `paulsha_cortex/porcelain/bootstrap.py` 實作（preflight + service/inspect 整合）。
- [ ] 1.3 `_FAMILY_MODULES` 登記 bootstrap 模組。
- [ ] 1.4 README 補「10 分鐘上手」段落（R-16）；`changelog.d/porcelain-bootstrap.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 porcelain-bootstrap 字樣）。
- [ ] 1.5 pytest 全綠、policy_check 0 fail、`git diff --check` 乾淨。
