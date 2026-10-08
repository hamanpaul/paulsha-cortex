---
status: accepted
work_item: release-user-level-profile
---

# release workflow 使用者層級發版模式規格

## Requirements

對應 [#1371](https://github.com/hamanpaul/paulsha-cortex/issues/1371)。owner 2026-10-08 裁決：0.1.14 以「使用者層級、序列派工」的穩定版發布，不支援 Trust Root system 部署，release 不封存 agent 執行檔。現行 `.github/workflows/release.yml` 一定要求 RC qualification，也一定會產出內含 agent toolchain 的 install-input。

1. **R1 profile 輸入**：`workflow_dispatch.inputs` 新增 `profile`，型別 `choice`，選項只有 `trust-root` 與 `user-level`，預設 `trust-root`。
2. **R2 trust-root 模式不變**：`profile == 'trust-root'` 時，所有 jobs、步驟、閘門與現行完全相同：RC qualification 查找、legacy-adoption、`qualification-gate`、install-input 與 manifest 的驗證和上傳都照舊。`tests/test_release_pipeline_workflows.py` 既有的斷言全部照舊成立。
3. **R3 user-level 的 preflight**：照常執行原始碼與治理檢查，包括 VERSION 與 tag、預設分支 head、policy 等；只跳過 RC qualification 的查找與 legacy-adoption 的要求，`rc_run_id` 與 `legacy_rc_run_id` 輸出為空字串。
4. **R4 user-level 的產物**：照常執行 `build` job，重現 wheel。不執行 `qualification-gate`。新增一個只在 `user-level` 下執行的 job 或步驟，把重現出來的 wheel 以 `qualified-dist` 以外的 artifact 名稱上傳，不產生 install-input 封存檔，也不產生 qualification manifest。
5. **R5 user-level 的 release**：`release` job 在兩種模式下都建立 annotated tag 與 GitHub Release，既有的 tag 目標、預設分支 head 等檢查照舊。`user-level` 只附 wheel，release notes 一定要包含這段固定聲明：`本版為使用者層級發版，不含 Trust Root 安裝輸入，不支援 Trust Root system 部署。`
6. **R6 ingress fail closed**：`paulsha_cortex/trust_root/install/release_ingress.py` 遇到只有 wheel、缺少 install-input 與 manifest 的 release 時，必須以 `ReleaseIngressError`（或模組既有的拒絕型別）拒絕，訊息要能看出缺的是哪個資產。若現行已是如此，只補測試，不改程式。
7. **R7 測試**：
   - 在 `tests/test_release_pipeline_workflows.py` 新增靜態解析 workflow 的測試：(a) `profile` 輸入存在、預設 `trust-root`；(b) RC 查找與 `qualification-gate` 只在 `trust-root` 下執行；(c) `user-level` 路徑不產生 install-input，release notes 含 R5 的固定聲明。
   - 新增或擴充 release ingress 測試，涵蓋 R6。

## Boundary

- 只改 `.github/workflows/release.yml`、`tests/test_release_pipeline_workflows.py`、release ingress 的測試。只有在 R6 需要時才改 `release_ingress.py`。
- 不改 `rc-qualification.yml`、`qualification/**`、`paulsha_cortex/trust_root/install/core.py`，也不移除 trust-root 模式的任何閘門。
- spec／design／todo 的文字是 pinned authority，只能勾選 checkbox。

## Evidence

2026-10-08 盤點 `origin/main`：`release.yml` 的 `release-preflight` 一定會查同一 SHA 的 `rc-qualification.yml` 成功紀錄；`qualification/prepare_candidate.py` 要求 toolchain 剛好包含 codex、claude、copilot、agy、srt、openspec。完整移除封存（#1293）牽動 `trust_root/install/core.py`（110 處相關引用）、`qualification/run.sh`（53 處）與約 40 個測試檔。
