---
status: accepted
work_item: release-pipeline
---

## Goals

建立 v0.1.0 可信賴發佈所需的 CI/release pipeline：多版本測試、封裝驗證、乾淨環境 smoke-install 與 tag 觸發的 GitHub Release（issue #95）。

## Why

porcelain 七家族陸續落地後，v0.1.0 需要可信賴的發佈流程證明「多個 Python 版本下皆通過測試、封裝產物可正確安裝」；目前 repo 只有單一版本的 `tests.yml`，缺乏 build 驗證與 release 自動化，也沒有面向使用者的 `pipx install` 之外的可信賴分發路徑（GitHub Release attach wheel/sdist）。

## What Changes

- 擴充 `.github/workflows/tests.yml`：Python 3.10–3.13 matrix。
- 新增 build job（`python -m build` + `twine check --strict`）與 clean-venv smoke-install job。
- 新增 `.github/workflows/release.yml`：tag `v*` push → build → GitHub Release 附 wheel/sdist；不上 PyPI。
- 所有新增／修改 `uses:` SHA pin（R-15）；不動 `policy-check.yml` 引擎 pin（R-23）；新 workflow 不宣告 `policy_version`（R-20）。

## Capabilities

### New Capabilities

- `release-engineering-pipeline`: v0.1.0 發佈工程契約——多版本測試矩陣、封裝驗證、乾淨環境 smoke-install 與 tag 觸發的 GitHub Release 自動化，不含 PyPI 發布。
