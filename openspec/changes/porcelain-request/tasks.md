---
status: accepted
work_item: porcelain-request
---

# Tasks

- [ ] 1.1 RED：`tests/test_porcelain_request.py`（tmp control root fixtures，四子命令 + `--json` + 退出碼）。
- [ ] 1.2 `paulsha_cortex/porcelain/request.py` 實作 + `_FAMILY_MODULES` 登記。
- [ ] 1.2b B1 findings 承接：`load_commands` fail-open + 明確錯誤；canonical registry spec 補 Purpose。
- [ ] 1.3 README 命令面補 request 家族段（R-16）。
- [ ] 1.4 `changelog.d/porcelain-request.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 porcelain-request 字樣）。
- [ ] 1.5 pytest 全綠、policy_check 0 fail、`git diff --check` 乾淨。
