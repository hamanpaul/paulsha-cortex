## Tasks

- [x] **T1 RED（tests）**：新增 `tests/test_ship_preflight_evidence_1366.py`，涵蓋 spec R5 的 (a)～(d)，確認在現行程式上 (a)～(c) 失敗。
- [x] **T2 實作（source）**：`_ship_action` 在 preflight 失敗時先寫 `cortex-pr-preflight/v1` evidence（`stage: "ship"`），再拋出帶 evidence 路徑的 `ship preflight failed: <stage>`；寫入失敗時依 design D3 處理。
- [x] **T3 回歸（tests）**：跑完整 `pytest`（記得 `env -u PYTHONPATH`），既有 `tests/test_ship_*.py` 全部通過。
- [x] **T4 文件（documentation）**：新增 `changelog.d/ship-preflight-evidence.md`，並同步 `CHANGELOG.md [Unreleased]`。
- [x] **T5 CLI 契約（R-16 cli）**：確認本票沒有新增或修改任何 CLI 參數與 help；若有修改，依 R-16 同步 CLI help 與文件。
