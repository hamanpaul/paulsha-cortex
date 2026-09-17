---
status: accepted
work_item: ship-lane-no-openspec
---

# Ship lane 支援無 openspec change 的 run；review-attest 在無 openspec／尚無 PR 時可用

## Boundary

- Issue：`hamanpaul/paulsha-cortex#911`。
- 範圍裁決（issue 給 A／B／C 三案）：**做 A＋C，不做 B**。A 落地後 `mapped_openspec == ()` 是
  ship lane 的合法交付模式，B 的「`openspec_refs` 空即 fail-loud」與之矛盾；`feature-oneshot`／
  `fix-standard` 的 `openspec-propose` 卡確定性 passed 卻無產物屬 planner lane 缺陷，另票處理，本票不動
  define 階段。
- 三種 combo 的既有事實：`small-fix` 沒有任何 openspec 卡；`fix-standard` 有 `openspec-propose`、無
  `openspec-archive`；`feature-oneshot` 兩張都有。本票只讓「`mapped_openspec` 恰好 0 個」走通；
  `== 1` 的既有 archive 路徑逐行不變；`> 1` 維持 `multiple-delivery-targets-unsupported` fail-closed。
- 觸及面（皆為 `paulsha_cortex/coordinator/`）：`work_actions._ship_action`（入口 gate、
  `_ship_binding`、`protected_refs`、本地 archive 段、`remote.active_openspec_absent／archive_present`
  檢查、`merged-awaiting-closure` 文案、`engineering_outcome` candidate）、
  `work_actions._review_attest_action`、`work_actions._validate_maintainer_review`、
  `delivery._validate_work_authority`、`delivery.ShipOrchestrator.merge_if_ready／verify_remote_closure`
  的 `change` 參數、`github_delivery.GitHubDeliveryClient._openspec_facts／fetch_delivery_facts／
  fetch_remote_closure／evaluate_final_gate／commit_merge`、`github_delivery.evaluate_delivery_gate／
  evaluate_remote_closure`、`completion._normalize_work_authority`、
  `work_bridge._commit_archive_and_require_reverification` 的呼叫端守衛。
- 不動：claim／resume 的 `openspec_refs_compatible`（#776）、planning lane、`openspec-archive` 卡本身、
  `abandon`／`retire-delivered` 語意、durable completion record 的 `schema` 版本字串（欄位允許 nullable
  即可，不 bump schema）、preflight／policy／CI-parity gate、Copilot review 迴圈與 repair budget（#218）。
- 不放寬任何非 openspec 的 fail-closed：PR 仍恰好 1、todo 仍恰好 1、issue closing keyword、
  merge ancestry、todo 全勾、completion record hash 全部照舊。無 openspec 模式的 remote closure 以
  「PR merged（雙親 merge commit）＋所有 mapped issue closed＋todo 全勾＋completion record 有效」為準。

## 現場證據

- #904 run `workflow-36924c59a745ff84a8d4`（small-fix）：build／verify／review 全過、PR #909 CI 綠，
  `_ship_action` 以 `len(mapped_openspec) != 1` 判 `multiple-delivery-targets-unsupported`
  （`prs=1 openspec=0 todo=1`）；小修 combo 沒有 openspec 卡，**任何 small-fix run 都不可能通過 ship**。
- #824 run `workflow-ef387ab2c1de1a354ae2`（feature-oneshot）與 #826 run `workflow-22b00a5937e88c0090ab`
  （fix-standard）：`openspec-propose` 卡確定性 passed 但 `openspec/changes/<work_id>` 從未產生，
  `openspec_refs: []`，同樣在 ship 被拒；attention `next_actions` 只給 `abandon`、`evidence_refs: []`。
- 三案最後都靠 operator 場外 `gh pr merge --match-head-commit` ＋ `retire-delivered` 收尾
  （PR #909／#910／#921），一條原本全自動走完的 run 最後一步一定需要人。
- `work_bridge` 的 ship 段已把 `change = mapped_openspec[0] if len(...) == 1 else None` 傳進
  `_ship_action`，但 `_ship_action`／`_ship_binding`／`_openspec_facts` 收到 `None` 立即拒絕；
  `_review_attest_action` 直接取 `authority.mapped_openspec[0]`，空 tuple 時 IndexError。
- 本 run 自身跑在 pin `811411ab`（舊 ship 程式碼）上，其 ship 同樣會停在
  `multiple-delivery-targets-unsupported`；預期由 operator 場外 merge ＋ `retire-delivered` 收，
  不視為本票缺陷。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_ship_lane_no_openspec_911.py`。以 `mapped_prs=(N,)`、
      `mapped_openspec=()`、`mapped_todo_paths=(todo,)` 的 authority 呼叫 `_ship_action(change=None)`，
      現行必須回 `needs_human: multiple-delivery-targets-unsupported`（RED 基線）；同 authority 呼叫
      `_review_attest_action` 現行必須 IndexError／RuntimeError（RED 基線）。修後：(a) `_ship_action`
      進入 binding（`change: None`）、跳過本地 archive、`ensure_pr_metadata`／preflight 照跑；(b)
      `mapped_openspec` 為 2 個仍 `multiple-delivery-targets-unsupported`，且 diagnostic 文案指出
      「`cortex work unlink` 修正 correlation 後 `resume`」；(c) `== 1` 的既有測試（`tests/test_work_actions.py`
      內 ship 段、`tests/test_delivery_orchestrator.py`、`tests/test_github_delivery.py`）逐條綠燈不改斷言。
- [x] **T2 source／ship 入口與 binding**：`_ship_action` 入口 gate 改為 `len(mapped_prs) != 1 or
      len(mapped_openspec) > 1 or len(mapped_todo_paths) != 1`；`_ship_binding` 在 `mapped_openspec == ()`
      時接受 `change is None`（有 openspec 時仍必須 `change in mapped_openspec`）；`protected_refs`
      只在 `change` 非 None 時加入 `openspec/changes/<change>`；本地 archive 段（`active_change.is_dir()`）
      在 `change is None` 時整段跳過；`remote.active_openspec_absent／archive_present` 檢查、
      `engineering_outcome` 的 `openspec_change` 與 ship journal 的 `change` 欄位允許 `None`；
      `merged-awaiting-closure` 文案在無 openspec 時不提「openspec archive」。`delivery_binding`
      持久化比對（`persisted_binding != binding`）對 `change: None` 冪等。
- [x] **T3 source／GitHub facts 與 gates**：`GitHubDeliveryClient._openspec_facts` 接受 `change=None`，
      回 `(True, True)`（無 openspec 即「active 不存在、archive 不需要」）並在 `DeliveryFacts`／
      `RemoteClosureFacts` 新增 `openspec_required: bool`；`evaluate_delivery_gate`／
      `evaluate_remote_closure` 只在 `openspec_required` 時產生 `active-openspec-present`／
      `openspec-archive-missing`；`fetch_delivery_facts`／`fetch_remote_closure`／`evaluate_final_gate`／
      `commit_merge` 的 `change` 型別放寬為 `str | None`，其餘 closure reason（issue closed、todo 全勾、
      merge ancestry、completion record）一律照舊。
- [x] **T4 source／delivery 與 completion**：`delivery._validate_work_authority` 把
      `not authority.mapped_openspec` 從 incomplete 條件移除、`len(mapped_openspec) != 1` 改為 `> 1`；
      `ShipOrchestrator.merge_if_ready／verify_remote_closure` 的 `change` 放寬為 `str | None`；
      `completion._normalize_work_authority` 允許 `mapped_openspec: []` 搭配 `change: null`
      （`mapped_openspec` 非空時仍必須恰好 1 且 `change == mapped_openspec[0]`），
      `completion_records_semantically_match` 與 strict reader 對 `change: null` 冪等；
      `work_bridge._commit_archive_and_require_reverification` 的呼叫端在 `change is None` 時不得進入
      （既有 `active_change is not None` 守衛即可，補測試釘住）。
- [x] **T5 source／review-attest（C）**：`_review_attest_action` 在 `mapped_openspec == ()` 時以
      `change=None` 呼叫 `fetch_delivery_facts`，不再取 `[0]`；放寬「必須已有 PR」——
      `run.pr_refs == ()` 且 `verified_head == candidate_head` 時仍可 attest，evidence body 的
      `pr_number` 為 `null`；`_validate_maintainer_review` 對 `pr_number is None` 的 evidence 只要求
      `candidate`／`run_id`／`authority_digest` 精確相符，由 ship 建 PR 後綁定（既有 `pr_number` 非 null
      的 evidence 仍精確比對 PR）。payload 新增選填 `evidence_refs: [{"kind": "operator-reproduction",
      "ref": "<absolute path>", "sha256": "<64 hex>"}]`，Manager 重算 hash 後原樣寫進 maintainer-review
      evidence body，不解讀內容；`allowed` 集合同步放行 `evidence_refs`，其他多餘 key 仍拒。
- [x] **T6 tests／回歸**：`tests/test_work_actions.py`、`tests/test_delivery_orchestrator.py`、
      `tests/test_github_delivery.py`、`tests/test_github_delivery_client.py`、
      `tests/test_coordinator_completion_record.py`、`tests/test_review_reviewer_attestation.py`、
      `tests/test_operator_adjudication_752.py`、`tests/test_engineering_outcome.py`、
      `tests/test_preflight_closeout_order.py` 全綠；補 T3／T4／T5 的斷言（含 `mapped_openspec` 為 2 個
      時 completion record 仍拒、`change` 與 `mapped_openspec` 不一致仍拒、`evidence_refs` hash 不符拒）。
- [x] **T7 documentation**：`docs/unified-work-lifecycle.md` 的 ship 段（「ship transition 現固定分成
      `local-closeout → pr-preflight → external-ship` 三段」與「Merge 後會重新 fetch default branch，驗證……
      archive……」）補「`mapped_openspec` 為空時跳過 official archive 與 archive facts，remote closure 以
      PR merged＋issue closed＋todo 全勾＋completion record 為準；`> 1` 仍 `multiple-delivery-targets-unsupported`」；
      `README.md` 的 `review-attest` 段補「無 openspec／尚無 PR 亦可 attest、`evidence_refs` 選填」；
      新增 `changelog.d/ship-lane-no-openspec.md` 並同步 `CHANGELOG.md [Unreleased]`。
