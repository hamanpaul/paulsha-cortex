---
status: accepted
work_item: release-pipeline
---

# release-pipeline Specification

porcelain 計畫（epic #84）B9：CI 測試矩陣與 release pipeline（issue #95）。本批次無對應 UX CLI 規格章節（`docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` 不涵蓋 CI/release），範圍以 issue #95 與 repo policy R-15/R-20/R-23 為準。

## Requirements

### 測試矩陣擴充

既有 `.github/workflows/tests.yml` SHALL 擴充為 Python 3.10–3.13 matrix，全版本 MUST 執行既有 pytest 套件。

### build 與 smoke-install

SHALL 新增 build job：執行 `python -m build` 產出 wheel/sdist 並以 `twine check --strict` 驗證封裝產物；SHALL 新增 clean-venv smoke-install job：於乾淨虛擬環境安裝 build job 產出的 wheel 後執行 `cortex --version` 與 `cortex --help`，兩者 MUST 皆以 exit 0 結束。

### release 自動化

SHALL 新增 `release.yml`：僅於 tag `v*` push 觸發，執行 build 後將 wheel/sdist 附加至對應 GitHub Release；MUST NOT 包含任何發布到 PyPI 的步驟。

### 引用 pin 與 policy 邊界

本批次新增／修改 workflow 的所有 `uses:` MUST 以 40-hex commit SHA pin（R-15）；MUST NOT 變動既有 `policy-check.yml` 的引擎 pin（R-23，避免與 policy attestation 衝突）；新增 workflow MUST NOT 宣告 `policy_version` 字面值（R-20 範圍外）。

### 檔案落點與 persona 邊界

本批次檔案變動落於 `.github/workflows/**`，不在既有 builder persona 的 `write_paths`（`paulsha_cortex/**`、`tests/**`、`openspec/changes/archive/**`）範圍內；MUST 以 persona shadow 模式允許寫入並讓 reviewer 知情此例外，不得靜默擴權。
