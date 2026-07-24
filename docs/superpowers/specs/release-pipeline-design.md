---
status: accepted
work_item: release-pipeline
---

# release-pipeline Design

## Goals

在不變動既有 `policy-check.yml` 引擎 pin 的前提下，補齊 v0.1.0 可信賴發佈所需的多版本測試、封裝驗證與 GitHub Release 自動化（issue #95）。

## Decisions

- **擴充既有 tests.yml 而非新建**：Python 3.10–3.13 matrix 直接擴充 `.github/workflows/tests.yml` 既有 job，避免與新 workflow 重複觸發同一段 pytest。
- **build/smoke 分離但以 artifact 銜接**：build job 產出 wheel/sdist 為 workflow artifact；smoke-install job 依賴該 artifact 於乾淨 venv 安裝驗證，不在 smoke job 內重複打包。
- **release.yml 觸發邊界唯一化**：僅 `on: push: tags: ['v*']`，與 PR/push-to-main 觸發的 `tests.yml` 互不重疊；不呼叫任何 PyPI 發布動作（no-goal 明文寫入 workflow 註解）。
- **SHA pin 格式統一**：所有新增/修改 `uses:` 一律 `owner/action@<40-hex-sha> # vX.Y.Z` 註解格式，比照既有 workflow 慣例（R-15）。
- **不動 policy-check.yml**：本批次 code_paths 涵蓋 `.github/**`，但明確排除 `policy-check.yml` 的引擎 pin 那一行，避免觸發 R-23 attestation 不一致。
- **persona write_paths 例外顯性化**：plan 與 PR 描述中明確註記本批次寫入 `.github/workflows/**` 超出 builder persona 既有 write_paths，需 shadow 模式放行並知會 reviewer。
