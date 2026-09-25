---
status: accepted
work_item: authorized-merged-run-proof-admission
domain_breadth: 1
state_consistency: 1
invariant_count: 11
artifact_classes:
  - source
  - tests
  - documentation
---

# Authorized merged-run proof admission（#975）

## Boundary

- Issue authority：[hamanpaul/paulsha-cortex#975](https://github.com/hamanpaul/paulsha-cortex/issues/975)。Parent：[ #962](https://github.com/hamanpaul/paulsha-cortex/issues/962)，祖先：[ #887](https://github.com/hamanpaul/paulsha-cortex/issues/887)。此票只負責 admission、phase-free binding、強證據 proof oracle。
- Dependency：#975 blocked by [#961](https://github.com/hamanpaul/paulsha-cortex/issues/961)。#976 等本票合併後實作受限 registry transition；#977 等 #975/#976 合併後實作 Manager finalization。三票完成後仍要回 #962 完成完整整合驗收。
- Parent AC ownership：本票負責 R1/R2 admission、R3 proof fail-closed、R4 helper/path、R6 proof portion、R8(a)–(d) admission/proof portion。#976 負責 R6 registry CAS；#977 負責 R6 completion/outcome/terminal orchestration與 R8(a)–(d) end-to-end。#961 保留 R5/R7/R8(e)–(f)。
- Production candidate：只改 `paulsha_cortex/coordinator/work_actions.py`、`paulsha_cortex/coordinator/manager.py`。預期新增供新舊 admission 共用的嚴格 phase-free journal binding 與 Manager internal read-only proof oracle；既有 reconciliation 保留最小判準，不碰 registry.py writer、completion writer 或 engineering_outcome.py。
- Tests：#961 合併後擴充 `tests/test_post_merge_authority_restart_guard.py`；以 temporary files 和 provider stubs，不呼叫 live GitHub。回歸 `tests/test_work_actions_retry_invalidation.py`、`tests/test_workflow_production_wiring.py`、`tests/test_work_actions.py`、`tests/test_work_claim.py`。
- Docs/changelog：實作 PR 加 `changelog.d/authorized-merged-run-proof-admission.md`、更新 CHANGELOG [Unreleased] 與 `docs/unified-work-lifecycle.md`；使用 PR context 跑 repo policy/tests。此次 planning artifacts 保持在 intake tmp。
- No-go：不建立 CompletionRecord/outcome，不改 WorkflowRun、gates、steps 或 verified_head，不新增 registry transition、persisted schema/field、CLI 或 public proof endpoint；不重做 #961 arbitration，不吸收 #976/#977 scope。
- Freeze：本目錄三份 `status: accepted` 文件是 #975 planning authority。實作時只勾 todo checkbox；若需改 acceptance、module boundary、ownership 或 persisted state，先停止並重新 review/sizing。

## 現況證據（2026-09-23，唯讀）

- Live #975/#962/#887/#961/#976/#977 已核對：全為 OPEN；#975 blocked by #961，#962 保留 aggregate，#976 位於 #975 之後，#977 依賴 #975 與 #976。
- Checkout 為 branch `main`、HEAD `ea3f81eff45be532d7f99155399b02d4e37be1a0`。存在八個既有 untracked `docs/superpowers/plans/*.md`；本次均保留不動。
- `work_actions.py:1913–1959` 的 claim-key mismatch branch 現行會 reset；舊 reset 將 claim key 同步並設 `authority_restart`，因此 verify-reset run 不會再次進入該 mismatch branch。
- `manager.py:11407–11452` 目前只讓 review、ship+done 進 merged-delivery reconciliation；`manager.py:11715+` 取得 current workflow step 後可派 verify，舊 verify run 必須在此之前攔截。
- `work_actions.py:1011–1130` 已有 authorization wrapper/hash/identity 檢查；`state_path` 由 `_run_state_path()` 指向 `delivery-journal.json`，work action 原值傳給 `_claim_action`。
- Repo sizing helper為 stability-risk-v2。fix-standard 輸入：9 cards、2 core gate_spine、9 persona bindings；適用規則為 R-09/R-16/R-19。三維 mechanical score：acceptance=2；accepted spec/design/plan 無 blocker 時 stability=0；orchestration=2。五維 1/1/2/0/2=6 Yellow。
- 以上只描述 source/issue/planning evidence，不宣稱 #961 已 merge、測試已跑、code 已改或 daemon/runtime 已切換。

## 五維 sizing

| 維度 | 分數 | Evidence |
|---|---:|---|
| domain_breadth | 1 | 兩個 production modules（work_actions.py、manager.py），同屬 coordinator。 |
| state_consistency | 1 | 跨本機 journal/auth/trusted evidence、WorkRun/WorkAuthority 與 remote PR facts 的唯讀精確 join；沒有本票 durable writes/CAS/crash window。 |
| acceptance_surfaces | 2 | 2 gate_spine + R-09/R-16/R-19 = signal 5。 |
| spec_stability | 0 | 三種規劃 artifact 均 accepted、無 missing kind、rejected artifact 或 blocker。 |
| orchestration | 2 | fix-standard 9 cards，9 張皆有 persona binding。 |
| **total / band** | **6 / Yellow** | **1+1+2+0+2=6；Yellow 上限 6。** |

Mechanical dimensions follow `planning.compute_sizing_score`: acceptance signal >2 maps to 2; complete accepted artifacts map to stability risk 0; cards>1 且 persona bindings>1 maps to orchestration 2。domain/state 是 plan frontmatter 宣告值，依本票兩 production modules 與唯讀跨來源 proof boundary宣告為 1/1。Issue body alone 不替代這三份 accepted artifact。

## Invariants（11）

1. 新 review/mismatch 與舊 verify-reset 是分開的 candidate predicates。
2. Journal helper 不含 phase 條件，使用 exact supplied state_path。
3. 既有 reconciliation 的 review 或 ship+done phase allowlist、最小 payload 判準與 raw JSON behavior 保持不變；其寬鬆 True 不得作為新 admission authority。
4. 新 mismatch 命中完整 binding 時在 reset 前 route closure，保留 retry classification，claim 不寫 registry。
5. 舊 verify-reset 只接受 ongoing/verify/authority_restart/claim-key-current，且在 pending verify dispatch 前攔截。
6. Journal 只作 admission hint，不證明 trusted gates、authorization 或 completion。
7. 原 authorization wrapper、canonical hash 與同一 run/PR/candidate identity 必須重讀核對。
8. Trusted foreign-review/preflight/checks/Copilot/maintainer-review evidence 必須沿現有 verifier 重驗。
9. Remote merge proof 綁同一 PR/head/merge commit，且 candidate 是 merge parent、merge commit 是 default branch ancestor。
10. 完整 binding 已知 merged 但 proof 失敗時 stop，不 dispatch 已知 merged candidate；binding 缺席/無效維持既有 fallback。
11. Proof/admission 不建立 durable completion、outcome、registry terminal state 或新的 persisted proof。

## Tasks

- [ ] **T0 — dependency and intake gate**：確認 #961 已合併後才做完整整合驗收；保留 #975 → #976 → #977 及 #962/#887 aggregate gate。驗收每個 implementation diff 仍只在兩個 production modules。
- [ ] **T1 — capture baseline regression**：確認 live #975/#962/#887 仍符合本 spec；保存現有 claim/reset/reconciliation/dispatch test baseline。確認原有八個 untracked plan 檔仍 untouched。
- [ ] **T2 — RED strict phase-free journal helper matrix**：在 test_post_merge_authority_restart_guard.py 覆蓋 exact state_path、raw JSON、phase-free behavior、schema/identity/head/SHA/auth locator/hash/payload/workflow_step_ids/PR/change/todo binding；workflow_step_ids 由 run.steps 推導，測 missing、symlink、non-regular、unreadable、bad JSON/encoding。另鎖定既有最小 payload fixture：舊 reconciliation=True、嚴格 helper=False，且不得藉舊結果放行新 admission。
- [ ] **T3 — RED new review mismatch route**：正例驗 resume/merged-delivery-closure、單行 safe info log、retry classification unchanged、無 reset/registry mutation/job dispatch；journal binding 負例維持既有 reset。檢查 helper 原 review/ship+done phase allowlist 與既有 fixtures不變。
- [ ] **T4 — RED old verify-reset pre-dispatch route**：只接納 ongoing verify + authority_restart + claim key 已同步 + exact journal binding；assert oracle 在 current pending-step dispatch 前執行，命中不派 verify、不更新 gates/registry。
- [ ] **T5 — RED proof oracle evidence matrix**：用 fixture 測 non-symlink/non-writable auth file、wrapper/payload/canonical hash、原 authorization digest、run/repo/work/steps/head/tree/PR/change/todo/review binding；negative 分別涵蓋 missing/symlink/writable/corrupt/mismatched authorization。
- [ ] **T6 — RED trusted evidence and remote proof matrix**：重驗 foreign-review、preflight/checks、Copilot/maintainer-review；覆蓋 evidence 缺失/損壞/identity mismatch、PR 未 merged、head/merge commit 不符、candidate 非 parent、merge commit 非 default ancestor、錯 source/PR/provider degraded。
- [ ] **T7 — GREEN strict admission journal binding**：新增 manager._merged_delivery_journal_bound(run, *, journal_path) 供兩條新 admission 路使用；既有 manager reconciliation 不委派它，保留原 phase gate、最小判準、輸入輸出和 fail-soft behavior。caller 將收到的 state_path 原值作 journal_path，不能用舊 helper 的寬鬆 True 代替嚴格 binding。
- [ ] **T8 — GREEN admission routing**：新 mismatch 在 reset 前 return closure route；舊 verify-reset 於一般 pending verify dispatch前 route proof oracle。對無完整 binding保留既有各自 fallback。
- [ ] **T9 — GREEN read-only proof oracle**：重讀 authorization/trusted evidence/同一PR遠端 merge ancestry；不得呼叫會寫 CompletionRecord 的 `ShipOrchestrator.verify_remote_closure()`，也不得偽造 `completion_record_valid` 套用 terminal gate。結果只在 Manager internal path使用且與run/head/auth hash綁定。proof failure對有效 merged row回診斷 stop，不派已知 merged candidate。
- [ ] **T9a — #977 consumer proof shape**：依 spec 完整回傳 run/current authority/original authorization/job-backed step/gate、Manager-only durable step provenance 與 CompletionRecord 本地 inputs 的 immutable、帶 ref/hash 的唯讀 proof；缺欄、跨 run/claim/job、Manager-only 無原生 durable proof、reset pending-only、重複/缺失 passed gate、source revision drift 均 typed stop。#995 fresh default_head 提供 `target_ref_sha`，本票不得用舊 base 偽造。正例可供 #977 組成經 validator 接受的 draft 與合法 terminal WorkflowRun，負例不得部分成功；spy 證明零持久化寫入。
- [ ] **T10 — no-write and integrated regression**：spy registry mutation、CompletionRecord/outcome append、gate/step writes；proof success/failure都不得呼叫。#961 合併後加入實際 reconciled WorkAuthority 整合 fixture，保留 Child A source-order/digest/fail-closed assertions。#962 的三次以上 re-entry 與 terminal state 結果由 #977 aggregate tests驗收。
- [ ] **T11 — docs, changelog and repository gates**：更新 lifecycle docs、child changelog fragment與 CHANGELOG [Unreleased]；跑本票 focused tests、指定回歸、required CI，並以 PR context跑 policy_check；記錄精確結果和未跑 gate。
- [ ] **T12 — child closeout evidence**：PR review/merge 後確認 #975 criteria均有對應測試/結果；提供 proof/admission slice 的結案證據，不宣稱 run 已 done、不關閉 #962/#887。

## 父票 AC ownership

| Criterion | #975 | #976 | #977 / #962 |
|---|---|---|---|
| #887 R1 | Admission/new mismatch | — | Existing closure integration |
| R2 | Detect and route old reset-to-verify before dispatch | Restricted state transition contract | Completion and final read-back |
| R3 | Journal/auth/trusted/remote proof rejection | CAS/partial-write rejection | No formal completion/outcome; diagnostic stop |
| R4 | Phase-free helper + exact path | — | Parent integration regression |
| R6 | Authorization/trusted evidence/remote ancestry proof | Manager-only atomic registry transition | Required delivery closure, CompletionRecord, outcome-first, retry/re-entry |
| R8(a)–(d) | Admission/proof cases | Registry-local positives/negatives | End-to-end #962 aggregate |

#975 does not complete #962 or #887. After #975, #976 and #977 still own their slices, and #962 must pass the entire R8(a)–(d) integration set after #961 is merged.
