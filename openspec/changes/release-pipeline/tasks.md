---
status: accepted
work_item: release-pipeline
---

# Tasks

- [x] 1.1 盤點既有 `tests.yml` 與待擴充範圍；本機 YAML/lint 驗證新 workflow 語法。
- [x] 1.2 擴充 `tests.yml` Python 3.10–3.13 matrix；新增 build job。
- [x] 1.3 新增 clean-venv smoke-install job；新增 `release.yml`（tag `v*` 觸發）。
- [x] 1.4 全新增／修改 `uses:` SHA pin；`changelog.d/release-pipeline.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 release-pipeline 字樣）。
- [x] 1.5 `tests/test_release_pipeline_workflows.py -q`、`python3 -m policy_check --repo .` 與 `git diff --check` 已重跑；authoritative preflight 已用 draft PR metadata 重現，現階段僅記錄本地 pre-archive 驗證，hosted CI / tag release 實跑留待 Manager 後續處理。
