---
status: accepted
work_item: fix-preflight-closeout-order
---

# fix-preflight-closeout-order Design

## Decisions

- 邊界重畫落在 ship validator 內部：固定三段 local closeout → review attestation →
  external ship mutation；`WORKFLOW_PHASES` 七段 spine 與 run schema 不動，workflow.py
  只新增 `SHIP_TRANSITION_STAGES` 常數與單調驗證函式。
- OpenSpec archive 段前移到 local closeout，不再被 `_ship_binding` 的 pr_number 硬性
  要求擋住；原 `_ship_action` 段保留為冪等 no-op 後盾。
- `_commit_archive_and_require_reverification` 移除內嵌 push——local closeout 零 external
  mutation；push／PR 建立／metadata／merge 全留 external 段沿用既有授權模型。
- preflight 失敗改回 trusted needs_human typed stop（`pr-preflight-blocked`），走
  `validate_ship_result` 既有 needs_human 通道，不落入 resume 例外 handler 的
  gate_status=failed 死巷；意外例外維持 fail-closed。
- review materialize 驗證延伸既有 `verify_authority_in_input_snapshot`（加可選
  workspace_root 做 post-materialize 實檔 sha256 驗證），並補齊 slice-based
  `prepare_review_worktree` 的 frozen authority materialize；不另建機制、不新增 row 欄位。
- #83／#175／#208 歷史教訓寫入風險節；與 #275 的 terminal transition 邊界列非目標。

詳細 D1–D6 與風險緩解見 `docs/superpowers/specs/fix-preflight-closeout-order-design.md`。
