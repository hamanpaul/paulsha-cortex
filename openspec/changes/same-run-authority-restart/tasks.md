---
status: draft
work_item: same-run-authority-restart
domain_breadth: 0
state_consistency: 2
invariant_count: 9
artifact_classes:
  - source
  - tests
  - documentation
---

# 既有 Candidate run authority restart／reverify 任務表（#1069）

## Boundary

Issue：[hamanpaul/paulsha-cortex#1069](https://github.com/hamanpaul/paulsha-cortex/issues/1069)。唯一 Work ID：`same-run-authority-restart`；正式 mapping 見 [`.cortex/work-items.yaml`](../../../.cortex/work-items.yaml)，own OpenSpec 為 [`same-run-authority-restart`](.)，規劃依據為 [spec](../../../docs/superpowers/specs/same-run-authority-restart-spec.md) 與 [design](../../../docs/superpowers/specs/same-run-authority-restart-design.md)。Parent #1055；硬前置 #1054 + #1063/#1064/#1065、#1068；#1068 再依賴 #966。#1070 是完整 PR/journal reconciliation 後續 child，消費 #983 conditional writer。本輪只建 planning bundle；不 intake/dispatch/實作/merge。

唯一 production module 預期為 `paulsha_cortex/coordinator/work_actions.py`。此 slice 只在 operator 明確 `resume` 時，以 fresh WorkAuthority 和 #1068 exact CAS reset 同一既有 Candidate run，並重驗原 Candidate/PR 後重跑 verify/review。delivery journal 僅可唯讀核對 exact row identity/revision；不得寫 journal、push/create/update/merge/close PR 或執行任何 delivery mutation。若 #1054/#1068 發布介面不足，或需第二 production module、registry primitive、registry writer、journal writer/cross-writer API，停止並 issue-backed re-scope。

## Tasks

- [ ] **T1 dependencies／唯一 authority**：確認 #1054 與 #1063/#1064/#1065 已 accepted/merged、#966 已 merged 且 #1068 transition API 已 landed；逐欄記錄本票實際消費的已發布介面和 revisions。未完成即保持 block，不 intake。
- [ ] **T2 explicit-only entry**：在現有 `work resume` Manager action 接同-run branch。operator 的 exact repo/work ID request 才能到達；periodic/automatic/start/intake 路徑不能 reset claim/source era 或派 recovery job。多 run/ambiguous target typed stop。
- [ ] **T3 fresh authority**：消費 #1054 typed admission 與 #1065 WorkAuthority；驗唯一 owner-published canonical Todo、matching issue/work provenance、具體 Tasks、#1064 最新 successful generation/freshness。保存 before/after sorted revisions、snapshot hash、provider revision、完整 authority digest；missing/stale/ambiguous/drift 一律停止。
- [ ] **T4 exact pre-CAS snapshot**：固定 registry revision、run/status/phase/retry classification/old claim/source、Candidate/verified head/gates/evidence refs/jobs/PR refs，檢查零 active job；唯讀核對現有 journal row identity/revision，authenticated read 核對唯一 open PR identity 與 head==Candidate。任何 unknown/conflict/mismatch 不改狀態。
- [ ] **T5 single #1068 CAS**：用完整 expected snapshot 與新 verified authority digest/source binding 呼叫 #1068 transition 一次。不得 direct registry update/reset、raw-file writer 或第二個 partial commit。Conflict、stale revision、不同 payload 重送、persist unknown 回 typed stop。
- [ ] **T6 post-CAS reread／same Candidate dispatch**：在派工前重新取得 successful/fresh authority、exact run/new claim era、原 Candidate、原 PR/head/state 和 no-active-job tuple。全數相符才只重跑 verify/review；build 不重跑。dispatch/evidence 綁同 run/repo/work/Candidate/new claim era。CAS 後 drift 不派工。
- [ ] **T7 evidence/history invariants**：驗 Candidate、build/repair、舊 JobRegistry rows、old claim bindings/evidence bytes/refs、PR refs/run ID 保留；僅 verify/review gate invalidated。舊 verify/review evidence 不得被採信為 new-era gate；不 write journal、不建立替代 run。
- [ ] **T8 real Manager fixture matrix**：用 stub GitHub reads 及 fixture run `workflow-52d048b72adbd5cae06f`／Candidate `7ba7e877c94ff4eee72ba796ea9f8962953ed5cc`／PR #1049 覆蓋 positive fresh/unique Todo、missing/ambiguous/stale Todo、publication digest drift、wrong tuple、active job、old evidence stale、concurrent resume、CAS conflict、crash before/after CAS、same/different-payload replay。GitHub/journal/push/PR mutation spies 全為零；不操作 live #983 run 或 PR #1049，fixture 與 live evidence 分開標示。
- [ ] **T9 docs／gates／handoff**：依 production implementation 實際變動同步 changelog fragment、Unreleased、README/docs/policy-required CLI help（如 scope 需要）；跑 focused/full tests、OpenSpec validation、official current sizing、PR-context policy、CI 和 exact-head review。PR body 保持 issue open，依 R-17 採用允許的 `policy-exempt:issue-link` 並寫清 planning-only 理由。合併、intake、deployment 或 live delivery 各自仍須後續明確授權與 gate。

## 五維 sizing

官方函式為 `work_bridge.current_sizing_snapshot(workspace_root, combo_name, artifact_rows)`；輸入採本 work item 完整 mapping 的 spec/design/plan views 與 `fix-standard`，適用規則固定 R-09/R-16/R-19。此分數只做 planning projection，不是 run sizing 或派工許可。

| Dimension | Value | Basis |
| --- | ---: | --- |
| domain_breadth | 0 | 預期僅一個 production module：`work_actions.py`；若正式實作需要另一 production module，重新 sizing/split。 |
| state_consistency | 2 | 同一 run 的 registry CAS、新 authority provenance、job/evidence era、PR/journal read-back 和 CAS 後 freshness 必須保持一致。 |
| acceptance_surfaces | 2 | `fix-standard` 2 個 gate_spine + R-09/R-16/R-19 共 5 個 signals，機械維度為 2。 |
| spec_stability | 2 | 本 bundle 目前 `status: draft`；官方 helper 以未 accepted artifacts 計風險 2。root review 接受且 artifacts 完整後才能把此維度重算為 0。 |
| orchestration | 2 | `fix-standard` 有 9 cards，且 9 張都有 persona_binding。 |

本 bundle draft 狀態的實際官方 sizing 為 **8 / Red（0+2+2+2+2）**；只有 root 接受完整規劃後才可報 issue 原先預估的 **6 / Yellow（0+2+2+0+2）**。Red 期間不 intake/派工；如果 root accepted-only 重算仍為 Red，保留全部 AC 並 issue-backed 再拆。

## Delivery status

此分支交付的是 draft planning authority 和唯一來源 mapping。Issue AC7 已核對為只允許唯讀 journal identity/revision read-back，禁止 journal write 與所有 PR/delivery mutation；#1070 接管完整 existing-PR journal authority read-back/conditional delivery。此規劃不代表 root acceptance、formal Cortex intake、產品 implementation、測試通過、merge 或 live #983/#1049 狀態變更。
