---
status: accepted
work_item: merged-run-completion-finalizer
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Manager merged-run completion finalizer Todo（#977）

## Boundary

- Issue：[hamanpaul/paulsha-cortex#977](https://github.com/hamanpaul/paulsha-cortex/issues/977)；parent #962，grandparent #887。唯一 work item `merged-run-completion-finalizer`。本 [spec](../../specs/merged-run-completion-finalizer-spec.md)／[design](../../specs/merged-run-completion-finalizer-design.md)／Todo 是 #977 的 accepted planning authority。
- 前置順序：#961 → #966/#967 → #975 → #976/#995/#996/#997 → #977。#975/#976 規劃在未合併 PR #985；產品整合必須等待全部前置實作合併及 consumer API shape 凍結。不得從 accepted 文檔推斷程式 API 已存在。
- Production 僅 `paulsha_cortex/coordinator/manager.py`；測試集中 `tests/test_post_merge_authority_restart_guard.py`。若第二個 production module 必需，停止、另開 issue-backed split、重算 sizing，不擅自吸收前置切片。
- 完成 #977 後仍需 #962 R1–R4/R6/R8(a–d) aggregate 與 #887 全部 AC。不得以本票關閉父票；不處理 #847/#885/#882、merge-authorized crash window、外部無授權 merge、monitor projection、一般 validator 放寬、新 schema/CLI 或部署。

## Evidence at planning intake

- 2026-09-25 唯讀核對 live #977/#962/#887、#995/#996/#997 及 PR #985（OPEN，head `071080ec6f783326762a40c94fbc47c748ddc49b`）；本規劃基準 `origin/main` `d399e5d531abba8cf5f649d54b6b02d0c48308b2`。#961 accepted triad 已在 main；#975/#976 accepted triad 僅在 PR #985，不能視為 merged runtime。
- #975 規劃的 consumer proof 分 `run_binding`、`work_authority_binding`、`merge_authorization`、`step_bindings`、`gate_bindings`、`completion_record_inputs`；#976 規劃的 restricted method 只驗 Registry-local binding。#995 規劃兩階段唯讀 closure；#996/#997 分別提供 conditional record create 與 canonical outbox CAS。產品實作要以合併後實際 API 再核對。
- main checkout 有其他未追蹤 planning 文件；本票使用隔離 worktree，未讀作 #977 authority、未改動它們。

## Five-dimension sizing

| Dimension | Score | Basis |
|---|---:|---|
| domain_breadth | 0 | 唯一 production module `manager.py`。 |
| state_consistency | 2 | record/outbox/registry 三個 durable boundary、external revalidation、crash/re-entry。 |
| acceptance_surfaces | 2 | `fix-standard` 2 gate_spine + R-09/R-16/R-19，signal=5。 |
| spec_stability | 0 | spec/design/todo 全 accepted、完整且無未解規格缺口。 |
| orchestration | 2 | `fix-standard` 9 cards、9 persona bindings。 |
| **Total / band** | **6 / Yellow** | repo `current_sizing_snapshot()` 對此 triad 的正式結果；不是 #962/#887 aggregate。 |

## Invariants（8）

1. 新 review mismatch 不 reset；舊 verify-reset 在一般 verify dispatch 前被安全辨認。
2. `needs_human` 只由 operator resume 穿越；已知 merged candidate 的失敗不重派 verify。
3. 每一個 terminal step/gate 皆由原始 run/authorization/job/evidence 唯一證明。
4. 每個 durable boundary 前重載 authority、重取 proof 與同次兩階段 closure。
5. CompletionRecord 完整 exact conditional create/reuse/read-back，既存 timestamp 固定。
6. OutcomeStore 用 injected canonical path、完整 payload CAS、既存 timestamp 固定且唯一 shipped row。
7. record/outcome exact durable 後才 #976 Registry-local terminal CAS；不聲稱跨 store transaction。
8. 三個 crash seam 可重入，負例不造假 outcome/done，既有 durable evidence 不被覆寫。

## Tasks

- [ ] **T0 dependency/API freeze**：核對 #961/#966/#967/#975/#976/#995/#996/#997 已合併及 API shape；逐欄對照 #975 proof、#995 same-snapshot default_head、#996 reader/receipt、#997 snapshot/CAS、#976 terminal binding。任一 consumer 欄位缺失或超出 manager.py production 範圍時，先停並 issue-backed 修正/重算。
- [ ] **T1 tests／RED admission**：在 `tests/test_post_merge_authority_restart_guard.py` 新增新 review mismatch 與舊 ongoing/verify/authority_restart/同 claim key/merged binding/no active job 路由；覆蓋 provider-backoff、periodic tick、operator_resume、`needs_human`、無 binding fallback 和已知 merged 不重派 verify。先證現有 source 不滿足舊 run 正例。
- [ ] **T2 source／fresh proof + closure**：僅改 manager.py 的 private finalizer；每 boundary 重載 WorkAuthority/source revisions、重取 #975 完整 proof、#995 第一階段 GET default_head、建立完整 draft、第二階段純驗證/evaluator。缺欄、identity/authority/default-head/Todo/PR/gate/evidence drift 停止；spy 禁止 side-effecting validator、ship flow 與任何隱性寫入。
- [ ] **T3 tests／CompletionRecord**：正例新建與既存 exact timestamp reuse；負例 malformed/symlink/writable evidence、unsafe read race、optional/source revisions 差異、writer hash/read-back mismatch。失敗零 outcome；不呼叫 legacy writer。
- [ ] **T4 source／record + outbox**：在每次重新驗證後使用 #996 conditional create/read-back，然後按 injected `OutcomeStore.path` 以 #997 canonical snapshot/expected revision conditional append；同 ID 先取固定 emitted_at 重建 expected payload，逐欄 exact reuse。collision/CAS conflict 本 tick 停止，下 tick 重新完整取證。
- [ ] **T5 tests／outbox concurrency**：以實際注入路徑、不同 default state path、相同 ID 不同 payload、重複 JSONL ID、unsafe final entry、source/default drift、stale revision 測零覆寫/零新 done；確認 #967 owner lock 不被用作 outbox writer 證明。
- [ ] **T6 source／restricted transition**：record 與 outcome exact durable 後、再次重新驗證全部外部/本地證據，再呼叫 #976；以 validated record projection 建 terminal binding、原 step/gate refs 與 verified_head。保留 Registry-local CAS/active-job/claim/generation gate，拒絕一般 writer/phase validator 或 retire-delivered 捷徑。
- [ ] **T7 crash/re-entry + aggregate evidence**：三個 boundary crash seam 各重試至少三次，斷言 record hash/bytes、單一 shipped outcome 完整 payload、done/ship zero-write same-binding 穩定；負例保留 durable evidence、診斷 stop、不重派已 merge candidate。#962 R8(a–d) 表逐項驗證，不將 R5/R7/R8(e–f) 冒領為本票成果。
- [ ] **T8 documentation/policy/PR gates**：實作 PR 更新 `docs/unified-work-lifecycle.md`、`changelog.d/merged-run-completion-finalizer.md`、CHANGELOG [Unreleased]；CLI help 不變。執行 focused 與列明的 regression tests、repo required pytest/openspec、PR-context `policy_check`、exact-head CI/review/mergeability；PR 只 `Closes #977`，不以本票關閉 #962/#887。將各 gate 結果分別報告。

## Re-evaluation

本次 accepted planning 只授權 #977 Manager integration；正式 intake 前重算 `current_sizing_snapshot()`。若前置合併改變 proof/closure/conditional writer/registry API，先核對是否仍完整；需加第二 production module、schema 或新 writer 時重新拆票與 sizing，不以本 6/Yellow 沿用。
