---
status: accepted
work_item: release-user-level-profile
domain_breadth: 0
state_consistency: 0
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# release workflow 使用者層級發版模式（#1371）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1371`；[spec](../../specs/release-user-level-profile-spec.md)、[design](../../specs/release-user-level-profile-design.md)。
- 只改 `.github/workflows/release.yml` 與相關測試；只有 spec R6 需要時才改 `paulsha_cortex/trust_root/install/release_ingress.py`。
- 不改 `rc-qualification.yml`、`qualification/**`、`paulsha_cortex/trust_root/install/core.py`，也不移除 trust-root 模式的任何閘門。
- spec／design／本 todo 的文字是 pinned authority，只能勾選 checkbox；需要澄清時寫進 terminal reason。

## Tasks

- [ ] **T1 RED（tests）**：在 `tests/test_release_pipeline_workflows.py` 新增 spec R7 (a)～(c) 的測試，並新增 release ingress 缺資產的測試（R6），確認 (a)～(c) 在現行 workflow 上失敗。
- [ ] **T2 workflow（source）**：新增 `profile` 輸入；依 design D2 以 `if:` 分流 RC 查找、legacy-adoption、`qualification-gate` 與 user-level 的 wheel 上傳；`release` job 依 D3、D4 在 user-level 下只附 wheel，並寫入固定聲明。
- [ ] **T3 ingress（source）**：若 T1 的 ingress 測試在現行程式上已經通過，就不改程式；否則讓缺資產時 fail closed。
- [ ] **T4 回歸（tests）**：跑完整 `pytest`（記得 `env -u PYTHONPATH`），`tests/test_release_pipeline_workflows.py`、`tests/test_qualification_release_gate.py`、`tests/test_trust_root_install_release_ingress.py` 全部通過。
- [ ] **T5 文件（documentation）**：在 `docs/` 下既有的發版說明文件補一段 user-level 發版的用法與限制，沒有合適的文件就新增 `docs/release-user-level.md`（不改 README，避免與 #1368 衝突）；新增 `changelog.d/release-user-level-profile.md`，並同步 `CHANGELOG.md [Unreleased]`。
- [ ] **T6 CLI 契約（R-16 cli）**：確認本票沒有新增或修改任何 cortex CLI 參數與 help（release workflow 的 `profile` 是 workflow_dispatch 輸入，不是 CLI）；若有修改，依 R-16 同步。
