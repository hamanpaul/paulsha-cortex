---
status: accepted
work_item: add-cortex-version-flag
---

# Tasks

- [x] 1.1 新增 `tests/test_cli_version.py`，確認現況 RED。
- [x] 1.2 `paulsha_cortex/cli.py` 頂層 `--version` 實作（importlib.metadata + fallback），測試轉 GREEN。
- [x] 1.3 README CLI 段落補 `--version`；`changelog.d/86-cortex-version-flag.md` 與 `CHANGELOG.md [Unreleased]` 同步。
- [x] 1.4 `pytest` 全綠、`policy_check` 0 fail、`git diff --check` 乾淨。
