---
status: accepted
work_item: authorized-merged-run-completion-finalizer
domain_breadth: 0
state_consistency: 2
invariant_count: 13
artifact_classes:
  - source
  - tests
  - documentation
---

# #977 Manager 冪等 verify-reset completion finalizer 工作計畫

唯一 owner：[hamanpaul/paulsha-cortex#977](https://github.com/hamanpaul/paulsha-cortex/issues/977)。

Planning views：[spec](../../specs/authorized-merged-run-completion-finalizer-spec.md)、[design](../../specs/authorized-merged-run-completion-finalizer-design.md)。依賴順序為 #961 WorkAuthority arbitration → #966/#967 Manager/registry owner → #975 proof oracle → #976 registry-local CAS、#995 pure closure inspector、#996 CompletionRecord conditional-writer、#997 OutcomeStore conditional-CAS→ 本票 Manager finalizer → #962 aggregate R8(a–d)。#961 不代表 CRecord/outbox writer serialization；#967 `jobs.json` owner lock 不保護不同 canonical outbox path；#976 不驗 external GitHub/default/Todo/OutcomeStore。保留 #887 全部 AC。前置票未合併、current authority reload path 未確認、或 consumer contracts 未凍結時不得開始 integration；#962/#887 不因 child unit pass 關閉。

## Boundary

未來 production diff 限於 `paulsha_cortex/coordinator/manager.py`。消費 frozen #975 proof、每次 fresh reload 的 WorkAuthority/source revisions、#995 pure closure inspector API、CompletionRecord conditional-writer API、OutcomeStore、#976 registry-local CAS。不得呼叫 `build_production_ship_validator`、`_completion_draft`、`ShipOrchestrator.verify_remote_closure`、`_ship_action`。不得改第二個 production module、registry schema/欄位、CLI、一般 registry writer 或 validator 契約；若必要即停止並另開 issue-backed split、重算 Red。

測試落在 tests/test_post_merge_authority_restart_guard.py。實作 PR 依 repo policy 加 changelog.d/authorized-merged-run-completion-finalizer.md、同步 CHANGELOG.md [Unreleased] 和 docs/unified-work-lifecycle.md，並做含 PR 上下文的 policy check。此計畫不代表 #975/#976/pure closure inspector 已合併、實作已完成或 runtime 已部署。

## Tasks

- [ ] **T1 contracts / intake gate (I4, I8, I9)**：核對 #975/#976/#995/CRecord-writer/#997 Outbox-CAS accepted API shape，確認 Manager 可 fresh reload current WorkAuthority/source revisions。已知 #975 僅綁 run/head/auth/trusted refs，故完整 CompletionRecord/WorkflowRun proof shape 仍是 blocker；要求 #975 owner freeze。確認 #976 只 local CAS、#995 先 validate draft 再 promote `completion_record_valid=True`、#996/#997 writer children 的 canonical path + exact CAS 契約；Outbox snapshot 由實際注入 `OutcomeStore.path` 取得。同步 live #977 body dependencies與 side-effect validator/legacy writer 移除要求；未凍結契約前停止，不猜 API。
- [ ] **T2 tests / RED eligibility and placement (I2, I3)**：在 tests/test_post_merge_authority_restart_guard.py 覆蓋 exact ongoing verify/authority_restart/claim identity；檢查 finalizer 位於 provider-backoff 後、needs_human/planning reconcile/ordinary verify dispatch 前。確認錯誤 phase/status/claim/head 不進 finalizer。
- [ ] **T3 source / Manager-only route (I1–I3, I9)**：在 manager.py 加 single-owner route，要求 #967 owner-locked daemon 路徑並用 per-run critical section 序列化 finalizer；精確辨識候選；無 needs_human 的 eligible tick 自動重驗；已有 needs_human 必須 operator_resume 才重驗。已知 merged candidate 失敗時不得 dispatch verify。
- [ ] **T4 tests / retryability and proof binding (I3–I4, I9, I12)**：覆蓋每個 durable boundary 前都 fresh reload authority/source revisions 並重新執行 proof；逐欄檢查原 authorization hash、workflow steps、step/job IDs、review/verify evidence refs、PR、source revisions；證明沒有合成 passed gate。retryable provider error 不持久化 needs_human；確定性 trust mismatch 保留 human diagnostic。
- [ ] **T5 source / pure closure integration (I5, I9, I10)**：每次邊界前 fresh reload WorkAuthority/source revisions 和 proof；從 proof 建立記憶體 CompletionRecord draft，呼叫 inspector 得 fresh RemoteClosureFacts/expected authority。inspection 先 pure validate draft 再設 completion-record-valid flag，並確認 target_ref_sha 等於當次 default_head。禁止 side-effect ship validator、_completion_draft 與 _ship_action。PR/head/merge-parent/ancestry/issue/OpenSpec/Todo與 authority exact compare。
- [ ] **T6 tests / closure and default drift (I5, I10)**：覆蓋 PR unmerged/head mismatch/merge parent/ancestry/issue/OpenSpec/Todo/authority/degraded provider、target_ref_sha 與 fresh default_head mismatch；在 CRecord 後/outcome 前及 outcome 後/CAS 前分別注入 default-head/closure drift，斷言不進下一持久化步驟。
- [ ] **T7 source / CompletionRecord conditional create/reuse (I6, I9)**：每次 writer 前重新 fresh-load authority/source、proof、closure並比對 snapshot。existing path 用 conditional-writer no-follow exact reader，保留 fixed completed_at；建立/重用都走 conditional writer，不呼叫 legacy writer。比對完整 normalized payload、optional/volatile fields；衝突不覆寫、不隔離。
- [ ] **T8 tests / CompletionRecord crash and collision (I6, I11)**：測試完成 record 後 crash、unreadable/invalid/symlink existing path、只差 run/auth/head/steps/evidence/PR/merge/default/Todo/source revisions/optional fields 的 conflicting record；任一 conflict 都不得寫 outcome/CAS，重入 hash 穩定且原 bytes 不變。
- [ ] **T9 source / Outbox conditional append CAS (I7, I9)**：append 前重新 reload authority/source、proof、closure並 exact compare CRecord。用與注入 registry/runtime configuration 一致的 explicit `OutcomeStore.path`，不得使用脫鉤 default `_run_state_path`；取 conditional-CAS child safe canonical snapshot/revision。若該 outcome ID 存在，安全取 existing fixed `emitted_at`，以 fresh proof 重建完整 expected payload並逐欄比較；不存在才新產生一次 emitted_at。僅 complete exact row（含 emitted_at）可零寫重用，再呼叫 `append_if_exact_or_create(expected_revision=...)`。mismatch/stale revision stop；同 ID 競爭 collision 後下一 tick 必 fresh proof/closure/snapshot 並依 winner fixed timestamp 重建比較，禁止沿用 `_now_iso` 新值誤判 crash retry。#967 owner lock 不視為 outbox CAS。
- [ ] **T10 tests / outcome fault and collision (I7, I11)**：注入 append crash、同 ID 錯誤 candidate/PR/merge/Todo/auth/record hash/source revisions；既有 row 正例須以 existing emitted_at 建 expected payload，確認 timestamp 不因 `_now_iso` 改變；不存在 row 才生成新 timestamp。確認每列 schema-invalid、duplicate outcome_id、final target pre-open/post-open symlink swap 都 fail closed，不寫 outcome/CAS；只接受完整相同 row，且 mismatch 不轉 terminal。
- [ ] **T11 source / Manager external compare then #976 local CAS (I8, I9)**：#976 呼叫前最後一次 reload authority/source、重跑 proof/closure，Manager exact compare remote/default/Todo/record/outcome。只傳 frozen API 支援的 expected local run/job/claim/head/auth state 與 terminal fields；#976 只檢 Registry-local run/job state/active-job/supported terminal fields並 CAS，不跨讀 remote/outbox。禁止普通 registry writer。
- [ ] **T12 tests / idempotent crash and CAS drift (I8–I12)**：對 CRecord/outcome/#976 三個 crash 邊界各重入至少三次；每次 fresh authority/proof/closure refresh、CRecord hash 不變、outcome 恰一筆、exact #976 API no-op 零新增 registry write。active job/head/auth/state revision/default-head/source-revision drift stop 且不 force update。
- [ ] **T13 regression / aggregate (I13)**：保留既有 closure、done resume、retry invalidation、production wiring、work action/claim、provider scope 530、superseded recovery、daemon tick isolation 的全部 assertions；保留 #975/#976/#995/#996/#997/pure-inspector/conditional-writer tests。不得改新 mismatch review→ship 或 retire-delivered。#962 繼續完整驗 R1–R4/R6/R8(a–d)，#887 全部 AC 繼續有效。
- [ ] **T14 documentation / policy and required gates**：新增 changelog fragment，更新 CHANGELOG.md [Unreleased] 與 docs/unified-work-lifecycle.md；確認無 CLI surface 變更，帶 PR title/body/labels/base/head 實跑 policy check，執行 repo required tests。
- [ ] **T15 closeout accounting**：分開報告 local implementation, focused/required tests, PR exact head/review/remote CI, merge 與 installed/runtime evidence；#977 completion 不代表 #962/#887 aggregate closure 或 deployment。

## 五維 sizing

以 repo work_bridge.current_sizing_snapshot 對本 plan、accepted spec/design、fix-standard combo 及完整 ACCEPTANCE_SURFACE_RULES 正式實算：

| 維度 | 分數 | 推導 |
|---|---:|---|
| domain_breadth | 0 | #977 唯一新 production module 為 manager.py；#995 closure inspector `delivery.py`、#996 CRecord writer `completion.py`、#997 outbox CAS `engineering_outcome.py` 都由獨立前置票提供。 |
| state_consistency | 2 | 跨 delivery journal、authorization/trusted evidence、CompletionRecord、outcome outbox、WorkflowRun/JobRegistry；有 crash windows、exact collision compare 與 CAS。 |
| acceptance_surfaces | 2 | fix-standard 有 2 gate_spine，加 adapter 固定 R-09/R-16/R-19，signal=5。 |
| spec_stability | 0 | 本三份 artifacts accepted，完整性 gate 無 blocker。 |
| orchestration | 2 | fix-standard 共 9 cards，9 個 persona bindings。 |
| **合計** | **6 / Yellow** | **0+2+2+0+2=6**。 |

若把 closure inspector、conditional CRecord writer 或 conditional outbox CAS 抽取併入 #977，domain_breadth 至少升為 1，依 helper 真值至少 7 / Red；不得沿用本表或削減 #962/#887 acceptance。現在由獨立 children 提供後，本票 production scope 保持 `manager.py` 單模組。current WorkAuthority reload 或 #975 proof shape 未確認時暫停 integration；OutcomeStore 必須使用其自己的 canonical path/revision CAS，不能以 #967 lock代替。dependencies/contract/completeness 變更時重新實算；Red 時如實回報、不偷降 state/AC。Declared invariant_count=13 與 artifact_classes 不代表資源封套可用。

## Completion accounting

#977 完成只代表 Manager finalizer 自身及必要 gates 通過；不代表 #962 aggregate R8(a–d) 完成，也不代表 #887 完成。merge、installed daemon、live recovery run、production delivery 各自提供獨立證據。
