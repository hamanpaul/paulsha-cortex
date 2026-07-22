---
status: accepted
work_item: porcelain-inspect
---

# Tasks

- [x] 1.1 RED：`tests/test_porcelain_inspect.py`（六子命令、human/json 一致性、殭屍偵測情境、exit code）。
- [x] 1.2 `paulsha_cortex/porcelain/_runtime_probe.py` 共用探測函式實作。
- [x] 1.3 `paulsha_cortex/porcelain/inspect.py` 六子命令實作 + `_FAMILY_MODULES` 登記。
- [x] 1.4 README CLI 段落補 `inspect` 家族（R-16）；`changelog.d/porcelain-inspect.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 porcelain-inspect 字樣）。
- [x] 1.5 修補後 candidate 已重跑 `python3 -m pytest tests/ -q`、`python3 -m policy_check --repo .`、`git diff --check`。
- [x] 1.6 以 conventional commit 提交 tested descendant candidate（僅涵蓋 pre-archive builder repair）。
