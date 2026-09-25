---
status: draft
work_item: existing-pr-journal-authority-readback
issue: 1070
parent: 1055
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# 既有 Candidate／PR 的 delivery journal authority read-back（#1070）

## Boundary

- 唯一 Work Item：existing-pr-journal-authority-readback；issue：#1070；parent：#1055。唯一 mapping 在 .cortex/work-items.yaml；唯一 OpenSpec change 是 existing-pr-journal-authority-readback。
- 規範文件：[spec](../../specs/existing-pr-journal-authority-readback-spec.md)、[design](../../specs/existing-pr-journal-authority-readback-design.md) 與 [OpenSpec](../../../openspec/changes/existing-pr-journal-authority-readback/proposal.md)。
- #1069 負責 explicit same-run recovery eligibility、fresh authority、#1068 CAS、verify/review dispatch；依 root owner handoff，#1069 對既有 journal 僅唯讀核對 row identity/revision。#1070 才負責消費 #983 conditional-write API，讀回／必要時更新既有 Candidate/PR row 的 authority provenance。不得重判 #1069 reset eligibility。
- production scope 限 paulsha_cortex/coordinator/work_actions.py。#983、#1068、#1054/#1063/#1064/#1065 landed 才可 intake；正式 intake 前要指定不同於 #1068/#1069 的 delivery-journal integration maintainer，並將 #1070 與 #983/#1015 work_actions.py 變更序列化。

## Dependency snapshot

依 2026-09-25 live issue/PR checks，#983、#966、#1054、#1063、#1064、#1065、#1068、#1069、#1055 均 OPEN，#1068 明示 blocked by #966；PR #1049 是 OPEN/DIRTY。這個 planning packet 本身維持 draft，不代表依賴 landed 或可 intake。依賴未清之前，不派 Builder、不執行 Cortex work intake，也不操作正式 #983 run 或 PR #1049。

## Five-dimension sizing

| Dimension | Value | Basis |
| --- | ---: | --- |
| domain_breadth | 0 | 唯一 production module 為 work_actions.py；不含 #1069 eligibility、registry 或 GitHub client。 |
| state_consistency | 2 | 既有 durable journal row 的 revision CAS、authority provenance 更新、結果 read-back 與 crash/re-entry。 |
| acceptance_surfaces | 2 | fix-standard 有 2 個 gate_spine，加 R-09/R-16/R-19，共 5 個 signal。 |
| spec_stability | 2 | 目前 spec/design/todo 均 draft；helper 對未 accepted planning authority 加保守風險。 |
| orchestration | 2 | fix-standard 為 9 cards、9 persona bindings。 |

以 repo official work_bridge.current_sizing_snapshot(workspace_root='.', combo_name='fix-standard', artifact_rows=[spec, design, plan]) 實算目前 draft triad 為 (8, 'red')，維度 0 + 2 + 2 + 2 + 2 = 8 / Red。另以同一 helper 對暫存副本僅把 triad status 投影為 accepted，實算 (6, 'yellow')，維度 0 + 2 + 2 + 0 + 2 = 6 / Yellow。accepted projection 不是本分支狀態或 review 結果。Draft Red 時不 intake；保留全部 acceptance，待 owner/root review 後再決定是否接受 triad。

### Official sizing record

- Actual draft triad: (8, 'red'), measured by the repo helper on the branch files.
- Accepted-status projection in a temporary copy: (6, 'yellow'); the branch files remain draft.
- Combo evidence: fix-standard has 9 cards, 9 persona bindings, and 2 gate_spine.

## Invariants tracked (8)

- **I01 唯一責任交接：** #1069 的 journal check 為唯讀；#1070 擁有 existing-row authority reconciliation。
- **I02 Exact run identity：** run_id/repo/work_id/claim era/Candidate/PR/row identity 全程一致。
- **I03 Authenticated remote fact：** 原 repository 的原 PR 仍 OPEN 且 head==Candidate；不搜尋或重綁 PR。
- **I04 #983 CAS only：** 比較 exact durable revision 與 canonical payload；不造 writer 或新 store。
- **I05 Result preservation：** 保留原 transaction identity/result；已有 current authority 時 read-back no-op，不新增 event/time。
- **I06 Same-identity recovery：** conflict/unknown 只回 typed stop；unknown 僅做同 identity conditional read-back。
- **I07 Crash-safe replay：** fresh process 只見原 row 或唯一 confirmed update，不重複 journal event。
- **I08 No remote mutation：** push、PR create/update、merge、close spies 為零；#1049 DIRTY fixture 不前進 merge。

## Tasks

- [ ] **T01 前置落地與 owner：** 重驗 #983/#966/#1054/#1063/#1064/#1065/#1068/#1069 最新 issue/PR/head/CI；確認所有硬依賴已 landed 到 main。指定 delivery-journal integration maintainer，與 #1068/#1069 owners 不同，並序列化 #983/#1015 的 work_actions.py 工作。
- [ ] **T02 驗證接口邊界：** 核對 #1069 輸出只含唯讀 journal identity/revision，且 same-run gate 已完成；核對 #983 已 landed API 確實支援 exact row identity 下的 conditional read-back/update。缺少任一契約即停止、issue-backed 修訂，不自建 primitive。
- [ ] **T03 Exact tuple 與 remote facts：** 在 work_actions.py 消費既有同 run 的結果；對 exact run_id/repo/work_id/claim era/Candidate/PR/row tuple 及 authenticated 原 PR OPEN/head==Candidate 做新鮮核對。沒有搜尋或替換 PR 的 fallback。
- [ ] **T04 Journal authority reconciliation：** 以 #983 API 讀 durable revision、row identity 與 canonical payload hash。current authority/result 已存在時回 durable result 並 no-op；必要更新時只 conditional update 原 row 的 #1054 驗證 authority digest/vector，保留 transaction result、identity、時間與其他 row。
- [ ] **T05 Idempotency 與 typed stops：** 驗 same identity/same digest replay；改 payload、stale revision、missing/conflicting row、writer conflict、PR/head drift 與 authenticated read unknown 都 fail-closed。unknown 僅用同 identity conditional read-back，無法確認則停止。
- [ ] **T06 Crash/re-entry matrix：** 覆蓋 row load、conditional update、durable confirmation、fresh read-back 前後的重啟；fresh process 只得原 row或唯一 confirmed update，沒有 duplicate event/time/result。
- [ ] **T07 Fixture-only regression：** 用 workflow-52d048b72adbd5cae06f、Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc、PR #1049 建立 stub fixtures，不讀寫 live run/PR/journal；OPEN/DIRTY fixture 維持 fail-closed。
- [ ] **T08 Side-effect and production boundary：** push/create/update PR/merge/close spies 全為零；production diff 僅 work_actions.py。若需改 #983 API、schema、github_delivery.py、registry.py 或另一 production module，停止並重新 issue/sizing。
- [ ] **T09 Delivery evidence：** focused 與 repo-required tests、configured preflight/OpenSpec、PR-context policy、exact-head CI、review、mergeability、merge 後 runtime/母票 closure 分開記錄。只有本票自己交付的 planning packet 勾選；產品/遠端 gates 在實作 PR 保持未完成。
