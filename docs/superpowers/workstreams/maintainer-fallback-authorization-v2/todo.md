---
status: accepted
work_item: maintainer-fallback-authorization-v2
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Maintainer fallback 以 content-addressed v2 授權取代同 run/head 既有 v1（#871）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#871`；[spec](../../specs/maintainer-fallback-authorization-v2-spec.md)、[design](../../specs/maintainer-fallback-authorization-v2-design.md)。
- 觸及模組（1 個 production 模組 → `domain_breadth: 0`）：`paulsha_cortex/coordinator/work_actions.py` 的 `_merge_authorization_body`、`_authorization_record`、`_authorization_identity_matches`、`_ship_with_maintainer_review`（必要時新增同檔私有 helper）。`state_consistency: 1`：只新增 v2 content-addressed 檔名、v2 兩個選填欄位與 journal `superseded_merge_authorization` 一個欄位，沿用既有「先寫 immutable evidence、再 `_save_runs`」順序，不新增跨物件 CAS。
- 不改 `completion.py`（CompletionRecord `trusted_evidence_refs` 維持恰好 4 筆、恰一種 review kind；「closure 同時列 v1 與 v2」屬 CompletionRecord schema 變更，另案）、`delivery.py`、`work_bridge.py`、`manager.py`、CLI、`review-attest` payload；不改 Copilot 路徑 v1 檔名與 persisted 比對；不處理「journal 已是 v2 後改走 Copilot」反方向與同 review kind 內 `checks_hash`／`preflight_hash` 漂移（皆維持現行 fail-closed）；不放寬 `_ship_action` `merge-authorized` 前置檢查（`work_actions.py:5793-5809`）——`merge-authorized`＋v1 且 WorkAuthority digest 已前進時維持現行 `ship merge-authorized state malformed`（該 gate 依 `docs/unified-work-lifecycle.md` 不使用 terminal authority 例外、Copilot 路徑同樣被擋，與 v1／v2 並存無關；#871 實際走 `needs_human`／`copilot-review-timeout` 重入，另案）；不掃磁碟 orphan v1；不新增任何刪除或改寫 immutable evidence 的 action。
- spec／design／本 todo 文字是 pinned authority，只准把 `[ ]` 翻成 `[x]`；其他任何文字不得修改，澄清寫進 terminal reason。
- 留在 Manager checkout 給你的分支上工作，不得另建 `wt/...` 分支。
- 不得 commit 或刪除 `docs/superpowers/plans/maintainer-fallback-authorization-v2.md`。
- 測試與文件不得 hard-code `openspec/changes/<change>/` 路徑。

## 現場證據

- 2026-09 Hippo `hamanpaul/paulsha-hippo#149`（run `workflow-131d7d44bc4bdf0eac34`、work item `issue-146-task-memory-payload`）：同 `(run, candidate)` 已有 v1（Copilot）授權；`review-attest` 成功；`work resume` → `RuntimeError: merge authorization evidence conflict`，run 停 `needs_human`（`review-advance-failed`），PR 未合併，除 `abandon` 外無官方出口。
- 現行 main `7fa4716b`：`_authorization_record`（`work_actions.py:938-983`）不分 schema 以 `{run_id}-{head}.json` 為檔名（:953），內容不同即 raise（:961、:974）；`_ship_with_maintainer_review` 在 :1424 寫 v2、:1437-1439 對 journal 既有 `merge_authorization` 不等即 raise；Copilot 路徑 v1 merge 失敗後 ship 留在 `merge-authorized`，下一次 resume 經 `ReviewLoop.record_review` 逾時（`delivery.py:224-227`）於 :6167 以 `{**ship, ...}` 落 `copilot-review-timeout` 並保留 v1——修掉檔名衝突後第二道比對仍會擋。
- `completion.py:180-209` 要求 trusted evidence 恰 4 筆且恰一種 review kind；`tests/` 無「v1 先存、同 run/head 再走 maintainer ship」測試。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_maintainer_fallback_authorization_v2.py`（沿 `tests/test_work_actions.py::test_ship_reenters_copilot_stop_through_bound_maintainer_review` 樣板：`_snapshot`／`_initialize_delivery_journal` 等 helper 在本檔自備、fake `GitHubDeliveryClient`／`ShipOrchestrator`、monkeypatch `_validate_foreign_review`／`load_preflight_command`／`run_preflight`），斷言逐條對應 spec R7 (a)–(j)；現行 main 必須 RED（(a)(b)(c) 為 `merge authorization evidence conflict`；(d) 無法完成 merge；(i) v2 pair 不被接受）；(g)(j) 為現行即成立的 fail-closed 護欄，修改前後都須通過。
- [ ] **T2 source／v2 content-addressed 檔名（R1、D1）**：`_authorization_record` 依 schema 分流——v1 維持 `{run_id}-{head}.json` 與既有衝突語意；v2 改 `{run_id}-{head}-{digest}.json`；其他 schema `ValueError("merge authorization identity malformed")`；寫入流程與回傳形狀不變，寫 v2 不觸碰 v1。
- [ ] **T3 source／superseded pair 產生與 replay 驗證（R3、R5、D2、D4）**：`_merge_authorization_body` 加 keyword-only `superseded_authorization`（僅 maintainer 分支可用，Copilot 分支帶值 → `ValueError`），有值時 v2 加 `superseded_authorization_ref`／`superseded_authorization_hash`；`_authorization_identity_matches` 對 v2 接受「無 pair」或「完整 pair」兩種 key 集合，pair 存在時以同檔 helper 驗 v1 檔 immutable、wrapper hash、schema v1 與 `run_id`／`repo`／`work_id`／`head`／`tree_hash` 相符；`_trusted_evidence_refs` 不改。
- [ ] **T4 source／maintainer 路徑接受 superseded v1（R2、R4、R6、D3）**：`_ship_with_maintainer_review` 在寫任何 v2 之前依 D3 解析 `existing`／`prior`：journal v1 → 以 `_authorization_identity_matches(..., terminal_reconciliation=True)` 驗證後退為 superseded；`prior` 驗證失敗、v1 與既有 `superseded_merge_authorization` 不等、既有 v2 payload 與重算 body 不等 → `RuntimeError("persisted merge authorization differs from current gate evidence")`，不寫 v2、不呼叫 `merge_if_ready`；成功時 journal 寫 v2 為 `merge_authorization`，有 prior 時加 `superseded_merge_authorization`。`_validate_maintainer_review`、foreign review hash、`evaluate_delivery_gate` 檢查順序與內容不變。
- [ ] **T5 tests／回歸**：`tests/test_work_actions.py`、`tests/test_copilot_review_adopt_existing.py`、`tests/test_ship_lane_no_openspec_911.py`、`tests/test_coordinator_completion_record.py`、`tests/test_work_bridge.py`、`tests/test_delivery_orchestrator.py` 全綠、不改既有斷言；補「Copilot 路徑 `_authorization_record(v1)` 路徑仍為 `{run_id}-{head}.json`」「無 superseded 時 v2 payload key 集合與現行相同」斷言；全套 `python3 -m pytest -q` 通過。
- [ ] **T6 documentation／changelog／CLI help**：新增 `changelog.d/maintainer-fallback-authorization-v2.md` 並同步 `CHANGELOG.md [Unreleased]` 一條（#871：v2 content-addressed、superseded v1 保留稽核、fail-closed 不放寬）；本票不新增 CLI，以 `python3 -m paulsha_cortex.cli work --help` help smoke 驗證輸出不變；`docs/unified-work-lifecycle.md` ship 段「若後續已由Manager綁定exact-HEAD maintainer evidence…」句後補一句：同 run/head 既有 Copilot v1 授權會被保留為 immutable superseded 稽核、maintainer fallback 另建 content-addressed v2 並在 v2 內綁定 v1 ref／hash；superseded v1 驗證只放寬 authority_digest（audit-only、不授予 merge 權限），`merge-authorized` 與 merge 前 gate 仍不使用 terminal authority 例外。
