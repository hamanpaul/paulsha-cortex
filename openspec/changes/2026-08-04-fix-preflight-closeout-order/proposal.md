---
status: accepted
work_item: fix-preflight-closeout-order
---

## Goals

重畫 ship lifecycle 邊界：本地 deterministic closeout（OpenSpec archive、archive commit、candidate reset）先於且獨立於 PR metadata preflight；PR metadata preflight 只在 PR-specific transition 執行且失敗可恢復；review workspace 的 frozen planning artifacts 一律 materialize 並以 hash 驗證 fail-closed（#263）。

## Why

exact-head review 完成後，resume 的 ship advance 進入 ship validator 即先建 PR metadata 並執行 initial PR-metadata preflight；無 PR binding 時直接失敗，例外被 resume 捕捉後設 `gate_status="failed"` 死巷。而本地可完成的 OpenSpec archive closeout 藏在 `_ship_action` 內部，其入口 `_ship_binding` 硬性要求 `pr_number`——pre-PR run 的本地 deterministic 閉合被外部 PR 前置條件結構性阻塞。同時 slice-based foreign review 路徑只 checkout candidate，frozen planning authority 完全沒有 materialize 與 hash attestation，reviewer 是否讀到凍結 authority 只能靠 prompt 宣稱（2026-07-30 dogfood #252～#254 實測重現；#208 已指出 reviewer input contract 在 dispatch 後才驗證的教訓）。

## What Changes

- ship validator（`build_production_ship_validator.validate`）重排為固定三段：local closeout → review attestation 確認 → external ship mutation；local closeout（archive gate 檢核、官方 archive、archive commit、candidate reset）在 `pr_refs` 為空且無 PR metadata 時即可完成，不動 `WORKFLOW_PHASES` 七段 spine 與 run schema。
- `workflow.py` 新增 `SHIP_TRANSITION_STAGES` 常數與單調前進驗證函式，供 validator 與 stop reason 語彙共用。
- `_commit_archive_and_require_reverification` 移除內嵌 push；push、PR 建立、`ensure_pr_metadata`、copilot request、merge 全部留在 external ship 段並沿用既有 operator authorization 模型——無 GitHub authorization 時零 external mutation。
- PR metadata preflight 只在 PR-specific transition 執行；失敗改回 trusted `needs_human` typed stop（`pr-preflight-blocked`，綁定 exact candidate 與 preflight evidence），resume 不再落 `gate_status="failed"` 死巷，status 顯示下一個合法 operator action（`awaiting-pr-authorization`）；意外例外維持既有 fail-closed。
- `verify_authority_in_input_snapshot` 延伸可選 `workspace_root` 參數做 post-materialize 實檔 hash 驗證；`prepare_review_worktree` 補 frozen authority materialize＋驗證，materialization 紀錄（相對路徑、sha256、source revision、candidate SHA）入 evidence 流，不新增 job row 欄位；reviewer verdict `authority_hashes` 回填不符維持 fail-closed。
- `docs/unified-work-lifecycle.md` 同步三段語意與停止點；與 #275（outcome contract terminal transition）的邊界列為非目標。

## Capabilities

### Modified Capabilities

- `governed-delivery-closure`：詳見 `docs/superpowers/specs/fix-preflight-closeout-order-spec.md` 的 Requirements 與 `docs/superpowers/specs/fix-preflight-closeout-order-design.md` 的 Decisions。
