---
status: accepted
work_item: porcelain-inspect
---

# Tasks

- [ ] 1.1 RED：`tests/test_porcelain_inspect.py`（六子命令、human/json 一致性、殭屍偵測情境、exit code）。
- [ ] 1.2 `paulsha_cortex/porcelain/_runtime_probe.py` 共用探測函式實作。
- [ ] 1.3 `paulsha_cortex/porcelain/inspect.py` 六子命令實作 + `_FAMILY_MODULES` 登記。
- [ ] 1.4 README CLI 段落補 `inspect` 家族（R-16）；`changelog.d/porcelain-inspect.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 porcelain-inspect 字樣）。
- [ ] 1.5 pytest 全綠、policy_check 0 fail、`git diff --check` 乾淨。
