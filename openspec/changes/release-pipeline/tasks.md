---
status: accepted
work_item: release-pipeline
---

# Tasks

- [x] 1.1 盤點既有 `tests.yml` 與待擴充範圍；本機 YAML/lint 驗證新 workflow 語法。
- [x] 1.2 擴充 `tests.yml` Python 3.10–3.13 matrix；新增 build job。
- [x] 1.3 新增 clean-venv smoke-install job；新增 `release.yml`（tag `v*` 觸發）。
- [x] 1.4 全新增／修改 `uses:` SHA pin；`changelog.d/release-pipeline.md` 與 `CHANGELOG.md [Unreleased]` 同步（條目含 release-pipeline 字樣）。
- [x] 1.5 `python3 -m pytest tests/ -q` 全綠、`python3 -m policy_check --repo .` 0 fail、`git diff --check` 乾淨；authoritative preflight 已以 PR metadata 重跑並確認無 release-pipeline Candidate code regression，剩餘阻斷僅為 runner 環境限制。
