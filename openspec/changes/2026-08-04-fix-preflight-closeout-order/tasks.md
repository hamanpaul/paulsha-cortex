---
status: accepted
work_item: fix-preflight-closeout-order
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/fix-preflight-closeout-order.md` 的 TDD RED 章節新增 `tests/test_preflight_closeout_order.py`，確認全部失敗。
- [x] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/fix-preflight-closeout-order-spec.md` 的 Requirements（R1–R6）；不動 `WORKFLOW_PHASES` spine、不改 external mutation 授權模型、不新增 job row 欄位。
- [x] 1.3 `changelog.d/fix-preflight-closeout-order.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#263）；`docs/unified-work-lifecycle.md` 同步三段語意與停止點。
- [x] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

pre-PR run 的 ship validate 完成 local archive closeout（archive、archive commit、candidate reset）且全程零 `gh`／`git push`；PR metadata preflight 失敗回 typed stop（`pr-preflight-blocked`），本地 closeout 結果保留、run 可直接 resume 重試而不落 `gate_status="failed"` 死巷；階段順序 closeout → preflight → ship 可觀測；兩條 review 路徑均 materialize exact frozen artifact set，hash drift、缺檔、缺紀錄與 verdict 回填不符全部 fail-closed；本地 archive 完成後建立 PR 仍銜接 current-head CI、foreign review、final attestation 與 ship；既有 delivery／work_bridge／review 測試不回歸。
