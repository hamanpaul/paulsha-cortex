---
status: accepted
work_item: fix-preflight-closeout-order
---

# fix-preflight-closeout-order Specification

#263：PR metadata preflight 過早阻塞本地 closeout，且 frozen planning artifacts 未可靠 materialize 到 review workspace。重畫 lifecycle 邊界：本地 deterministic closeout（OpenSpec archive、planning artifact freeze、hash 驗證）先於且獨立於 PR metadata preflight；review workspace 的 frozen authority materialize 一律以 hash 驗證 fail-closed。

## 背景

2026-07-30 auto-run dogfood（#252～#254）暴露兩個同源缺口：

1. exact-head review 完成後，`resume` 的 ship advance（`paulsha_cortex/coordinator/manager.py:6450-6474` 的 `apply_workflow_action` advance 分支）進入 ship validator（`paulsha_cortex/coordinator/work_bridge.py:1378` 的 `build_production_ship_validator.validate`）。validate 先建 PR metadata（`work_bridge.py:1416` 呼叫 `_pr_metadata`、`work_bridge.py:452` 定義），無 PR binding 時立即執行 initial PR-metadata preflight（`work_bridge.py:1427-1437`，失敗 raise `initial PR-metadata preflight failed`），例外被 `manager.py:6467-6473` 捕捉後設 `needs_human` 且 `gate_status="failed"`。而本地可完成的 OpenSpec archive closeout（`work_actions.py:2904-2924`）藏在 `_ship_action`（`work_actions.py:2645`）內部，其入口 `_ship_binding`（`work_actions.py:563`）硬性要求 `pr_number`——沒有 PR 就永遠到不了 archive 段。本地 deterministic 閉合被外部 PR 前置條件結構性阻塞。

2. frozen planning／report artifacts 在部分 reviewer workspace 以 untracked overlay、未完整 materialize 或缺 hash attestation 的形式存在。workflow reviewer sandbox 路徑已有完整機制（`manager.py:6050-6078`：`_workflow_input_snapshot` → `verify_authority_in_input_snapshot` → `_create_reviewer_sandbox` materialize → `_validate_workflow_input_snapshot` 重算 hash），但 slice-based foreign review 路徑（`paulsha_cortex/coordinator/review.py:226` 的 `prepare_review_worktree`、`manager.py:924` 呼叫端）只 checkout candidate，完全沒有 frozen authority materialize 與 attestation——reviewer 是否真的讀到 run 凍結的 authority 只能靠 prompt 宣稱。

`review.py:261-292` 的 `verify_authority_in_input_snapshot` 已是 frozen authority 驗證雛形（hippo #41 v3 教訓的產物）；本次延伸它，不另建機制。

## Goals

- pre-PR run 的本地 archive／closeout gate 可完成；ship 停在明確的 PR-binding／authorization 邊界，而非 `gate_status="failed"` 死巷。
- PR metadata preflight 只在需要 PR-specific transition 時執行。
- 無 GitHub authorization 時零 external mutation，status 顯示下一個合法 operator action。
- 所有 review workspace（workflow sandbox 與 slice-based worktree）materialize exact frozen artifact set，hash 驗證 fail-closed，紀錄可稽核。

## Requirements

### R1 本地 closeout 先於且獨立於 PR metadata preflight

ship validator（`work_bridge.py:1378` 的 `validate`）SHALL 以固定順序執行三段：(a) local closeout、(b) review attestation 確認、(c) external ship mutation。

local closeout 段（canonical untracked report 清理 `work_bridge.py:992`、archive gate 檢核 `work_actions.py:108` 的 `_validate_local_archive_inputs`、官方 `openspec archive`、archive commit 與 candidate reset `work_bridge.py:783` → `registry.py:1441`）MUST 在 `run.pr_refs` 為空且無 PR metadata 的情況下即可完成，MUST NOT 以 `_ship_binding`（`work_actions.py:563`）的 `pr_number` 或 `_pr_metadata` 成功為前提。PR metadata 建構或 preflight 失敗 MUST NOT 使已完成的 local closeout 結果失效或不可恢復。

### R2 PR metadata preflight 只在 PR-specific transition 執行且失敗可恢復

`_run_exact_candidate_preflight`（`work_bridge.py:1118`）的 metadata 模式 MUST 只在即將建立 PR 的 transition 執行（`work_bridge.py:1427`）；`--pr` 模式 MUST 只在既有 PR 需要 push／merge 的 transition 執行（`work_bridge.py:1501`、`work_actions.py:2933-2946`）。其餘階段 MUST NOT 執行 PR metadata preflight。

preflight 失敗 MUST 產生 typed、可恢復的停止（trusted `needs_human` 結果，reason 標示 `pr-preflight-blocked` 類語彙並綁定 exact candidate 與 preflight evidence）；resume 的 ship advance（`manager.py:6450-6474`）對此類停止 MUST NOT 設 `gate_status="failed"`，run MUST 維持可直接 resume 重試，不需 registry surgery。非 typed 的意外例外 MUST 維持現行 fail-closed（`gate_status="failed"`）。

### R3 無 GitHub authorization 時零 external mutation

local closeout 段 MUST NOT 執行任何 GitHub 或 remote mutation：`_commit_archive_and_require_reverification`（`work_bridge.py:783`）內嵌的 `_push_exact_candidate` 呼叫（`work_bridge.py:841`）MUST 移出，push 延後至 external ship 段由既有 push 路徑（`work_bridge.py:1438`、`work_bridge.py:1514`）承擔。

push（`work_bridge.py:560`）、PR 建立（`github_delivery.py:859` 的 `create_or_get_pull_request`）、`ensure_pr_metadata`（`github_delivery.py:956`）、copilot request 與 merge MUST 全部留在 external ship 段，沿用既有 operator authorization 模型（merge_authorization／operator resume）；順序調整 MUST NOT 導致自動開 PR、push 或 merge。

### R4 review workspace 必須 materialize exact frozen artifacts 並以 hash 驗證

reviewer dispatch 前，Manager MUST materialize 全部 frozen planning authority artifacts 到 review workspace，並以 workspace 實際檔案內容重算 sha256 與 frozen baseline 完全一致（延伸 `verify_authority_in_input_snapshot`，`review.py:261-292`；sandbox 路徑沿用 `manager.py:3316` 的 `_validate_workflow_input_snapshot` 與 `manager.py:6074` 的呼叫）。

materialization 紀錄 MUST 含相對路徑、content sha256、source revision（`run.source_revision`／`planning_source_revision`）與 candidate SHA；缺任一項 MUST NOT dispatch reviewer。slice-based foreign review 路徑（`review.py:226` 的 `prepare_review_worktree`、`manager.py:924` 呼叫端）MUST 取得同等 materialize＋hash 驗證，MUST NOT 只依 prompt 宣稱。

### R5 reviewer verdict 必須回填 attestation 且不符 fail-closed

run 有 planning authority 時，reviewer terminal MUST 回填 `authority_hashes`（`manager.py:4221-4283` 的 expected_authority_hashes 驗證、`review.py:364-375` 的 `_normalize_authority_hashes`）；缺漏、ref 集不一致或 hash drift MUST fail closed 拒絕 verdict，MUST NOT 接受 PASS。materialization 紀錄缺失時同樣 MUST NOT 接受 PASS。

### R6 untracked overlay 不得成為隱形 authority

materialize 到 workspace 的 seeds MUST 全部由 Manager 寫入（atomic、0o600、expect_absent；`manager.py:3191-3219`、`manager.py:4010-4031`）且逐檔在 input snapshot 與 evidence envelope（`_write_workflow_input_content`）有紀錄；紀錄之外的 untracked planning／report 檔 MUST NOT 被當作 review 或 closeout 的輸入 authority。ship 前 canonical untracked report 的 hash 驗證後移除語意（`work_bridge.py:992`）維持不變。

## 非目標

- 不動 `WORKFLOW_PHASES` 七段 spine（`workflow.py:14`）與 `WorkflowRun` 的 ship invariants 驗證語意（`workflow.py:534-557`）；邊界重畫發生在 ship transition 內部的階段順序，不新增 phase、不改 run schema 欄位。
- 不改變 external mutation 的授權模型（merge_authorization、operator resume、`_SHIP_CAPABILITY` 封裝）；不自動開 PR。
- #275（outcome contract）另案：W3 將依本票穩定的 closeout → preflight → ship 邊界接 terminal transition；本票只穩定邊界與 stop reason 語彙，不定義 terminal outcome contract 本身。
- 不處理 #83 的 worktree／branch GC 與 stranded-run reconcile、#175 的 rebase-candidate／re-claim PR 繼承（各屬另案）。
- 不改 preflight 命令內容本身（`preflight.py` 的 policy／ci-parity 兩段與 `--skip-tests` 語意）。

## 驗收面

- pre-PR run：ship validate 完成 local archive closeout（archive、archive commit、candidate reset），全程零 `gh`／`git push`；ship 停在明確 PR-binding 邊界且 status 顯示下一個合法 operator action。
- PR metadata preflight 失敗時本地 closeout 結果保留、run 可直接 resume 重試，`gate_status` 不落入 `"failed"` 死巷。
- 階段順序可觀測：closeout 動作先於任何 preflight 執行，preflight 先於任何 GitHub mutation。
- 兩條 review 路徑都 materialize exact frozen set；hash drift、缺檔、缺紀錄一律 fail-closed，reviewer verdict 回填不符 fail-closed。
- 本地 archive 完成後建立 PR，仍銜接 current-head CI、foreign review、final attestation 與 ship（`_ship_action` 的 remote archive 檢查 `work_actions.py:2955-2956` 不變）。
- 既有 delivery／work_bridge／review 測試全綠，不回歸。
