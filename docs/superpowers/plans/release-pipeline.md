---
status: accepted
work_item: release-pipeline
---

# release-pipeline Plan

## Tasks

### 1. 驗證前置

- [ ] 盤點既有 `.github/workflows/tests.yml` 現況與待擴充的 Python 版本矩陣；確認新增/修改 workflow 的 YAML 語法可通過本機 lint（`actionlint` 或等效 YAML 檢查）。

### 2. 實作

- [ ] 擴充 `.github/workflows/tests.yml`：Python 3.10–3.13 matrix。
- [ ] 新增 build job：`python -m build` + `twine check --strict`，輸出 wheel/sdist 為 artifact。
- [ ] 新增 clean-venv smoke-install job：安裝 build artifact 後執行 `cortex --version`／`cortex --help`。
- [ ] 新增 `.github/workflows/release.yml`：tag `v*` push 觸發 → build → 建立 GitHub Release 並附加 wheel/sdist；不含 PyPI 發布步驟。
- [ ] 本批次所有新增／修改 `uses:` 一律 SHA pin（R-15）；**不得**變動 `.github/workflows/policy-check.yml` 的引擎 pin（R-23）；新 workflow 不宣告 `policy_version`。
- [ ] **注意**：本批次檔案落點為 `.github/workflows/**`，超出 builder persona 既有 `write_paths`（`paulsha_cortex/**`、`tests/**`、`openspec/changes/archive/**`），需以 persona shadow 模式放行並於 PR 描述中明確告知 reviewer 此例外。

### 3. 同步與驗證

- [ ] 新增 `changelog.d/release-pipeline.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `release-pipeline` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail（含 R-15/R-20/R-23）；`git diff --check` 乾淨；新 workflow 於 CI 實跑全綠。
- [ ] 完成後勾選 `openspec/changes/release-pipeline/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
