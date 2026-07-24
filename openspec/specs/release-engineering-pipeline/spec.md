# release-engineering-pipeline Specification

## Purpose
TBD - created by archiving change release-pipeline. Update Purpose after archive.
## Requirements
### Requirement: release pipeline 必須驗證封裝產物可在乾淨環境安裝執行

CI MUST 新增 build job 產出 wheel/sdist 並以 `twine check --strict` 驗證；MUST 新增 clean-venv smoke-install job，於乾淨虛擬環境安裝該 wheel 後執行 `cortex --version` 與 `cortex --help` 並確認皆 exit 0。

#### Scenario: smoke-install 驗證

- **WHEN** CI 於乾淨虛擬環境安裝 build job 產出的 wheel
- **THEN** `cortex --version` 與 `cortex --help` 皆以 exit 0 結束
- **THEN** 若安裝或執行失敗，smoke-install job MUST 標示為失敗，阻止該次 CI 視為全綠

### Requirement: 新增 workflow 的 uses 必須 SHA pin 且不得變動既有引擎 pin

本批次新增／修改的所有 `uses:` MUST 以 40-hex commit SHA 加版本註解 pin；MUST NOT 變動 `.github/workflows/policy-check.yml` 既有的引擎 pin。

#### Scenario: release.yml 的 action pin

- **WHEN** 審查 `.github/workflows/release.yml` 新增的每一個 `uses:` 步驟
- **THEN** 每一個 `uses:` 皆為 `owner/action@<40-hex-sha>` 形式並附版本註解
- **THEN** `.github/workflows/policy-check.yml` 的引擎 pin 內容與變更前完全一致

