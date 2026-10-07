---
status: accepted
work_item: autosync-source-job-unify
---

# 自動同步的 candidate 來源 job 判定統一：review 收割與 repair build 也要認 autosync job

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1345`（#1336 未涵蓋的兩處）。
- 現況：#1336 讓自動同步後的 verification 卡能派出，但仍有兩處不認 autosync job：
  - **review 收割**：`manager._review_builder_job_binding` 只把 openspec-archive 的 manager job 當成替代 author，自動同步的 manager job（phase ship）判定 `review evaluation builder binding mismatch: workflow_phase`。
  - **repair build base**：`retry-build` 推導 repair build 的 base 時取同步前 builder job 的 subject_head，`seams.py` worktree `create()` 回 `existing worktree branch has commits outside requested base`。
- 不放寬 review 收割與 worktree 建立既有的安全檢查；只擴充「合法來源 job」的定義。

## Tasks

- [ ] **T1 RED**：以端到端測試重現兩個失敗：自動同步後 code-review 收割的 binding mismatch；自動同步後 retry-build 的 branch outside base。
- [ ] **T2 單一判定 helper**：抽出「candidate 的來源 job」判定 helper，`_workflow_stage_execution_builder_context`、`_review_builder_job_binding`、repair build 推導 base／branch 的地方一律共用；自動同步的 manager job 與 openspec-archive 同等對待。
- [ ] **T3 端到端**：clean-behind → 自動同步 → verification 通過 → code-review 收割通過 → 進入 ship；自動同步後 retry-build 能派出 repair build。
- [ ] **T4 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
