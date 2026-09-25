---
status: draft
work_item: verify-planning-drift-recovery
issue: 1046
domain_breadth: 2
state_consistency: 2
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# Verify planning-drift recovery Todo（#1046）

## Boundary

- 唯一 work item：verify-planning-drift-recovery；owner issue 是 [#1046](https://github.com/hamanpaul/paulsha-cortex/issues/1046)，父票 [#1042](https://github.com/hamanpaul/paulsha-cortex/issues/1042)，明確 blocker 是 [#1045](https://github.com/hamanpaul/paulsha-cortex/issues/1045)。規格與設計分別見 [spec](../../specs/verify-planning-drift-recovery-spec.md)、[design](../../specs/verify-planning-drift-recovery-design.md)。
- 規劃基準：2026-09-25 live #1042/#1045/#1046/#562 與 Draft PR #1048。#1045 仍 OPEN，PR #1048 仍 Draft 且官方 sizing 10/Red（accepted-only projection 8/Red）；receipt API 尚非可消費契約。#1042 保留 aggregate acceptance；#1046 不追認 #961 已 abandoned run。
- #562 管 planning runtime/BrainstormResult failure taxonomy；#1046 是 candidate 已存在、verify job 尚未建立時的 verify-dispatch planning drift stop。不得共用 substring classification 或擴張 define 專用 recover-planning。
- Manager 是唯一 source/receipt/job eligibility 判定者；adapter 只傳 repo/work/run/candidate selector；Registry 只執行 Manager 提供 expected-old tuple 的受限 CAS。候選 SHA 維持原值；recovery 不產生 passed verification/review evidence。#1045 receipt 的 schema/source API 必須先完成且 read-back 可重驗。
- 預期 production scope：coordinator/manager.py、coordinator/work_actions.py、coordinator/registry.py、受控入口 control/contract.py/porcelain/recover.py，以及只有在 #1045 source API 不足時才可增加的 WorkBridge 唯讀 source resolver。T0 若多出 production module、新 schema/writer 或 ownership boundary，停下重畫 scope 並重新 sizing。

## Five-dimension sizing

| Dimension | Draft score | Basis |
|---|---:|---|
| domain_breadth | 2 | 預估跨 Manager stop/eligibility/dispatch、WorkAction/selector、Registry exact CAS，並可能需受控 transport 與 WorkBridge source adapter；至少三個 production responsibility/module。 |
| state_consistency | 2 | 要串接 immutable #1045 receipt、structured stop、WorkAuthority source set、run generation/candidate、job inventory 和 atomic Registry CAS；包含競爭、crash/re-entry、read-back 及不可重複 dispatch。 |
| acceptance_surfaces | 2 | fix-standard 具 2 個 gate_spine；本 repo code_paths 適用 R-09/R-16/R-19，signal=5。 |
| spec_stability | 2 | 此三件套是 draft，且上游 #1045 receipt contract 尚未交付。 |
| orchestration | 2 | 本 repo packaged fix-standard 為 9 cards、9 persona bindings。 |
| **Total / band** | **10 / Red** | repo current_sizing_snapshot() 對本 draft triad、fix-standard 與 R-09/R-16/R-19 實算；不得 intake。 |

以同一份 draft triad 暫存副本僅將三份 frontmatter 的 status 改為 accepted 後，repo helper 投影為 **8 / Red**（其餘 dimensions 仍為 2/2/2/2）。這不是 review/acceptance receipt，也不授權 intake。分數結果以本 PR 實際執行的 helper 輸出為準；任何 source API、module、schema、gate 或 task scope 改變都必須重算。Red 不刪 AC、不降維、不派工。

## Invariants

1. 恢復只作用於 WorkAuthority 唯一授權的 exact repo/work/run/candidate。
2. verify dispatch 前的 planning drift 有穩定結構化 stop code 與精確 run/candidate/phase/source context。
3. 本票 stop 與 #562 planning runtime taxonomy 不混用；一般錯誤文字不是 authority。
4. 唯一審查與來源證據是同 run/work/card 綁定且 hash 可重驗的 #1045 ready receipt。
5. Manager 以同一受信任 WorkAuthority snapshot 驗證完整且唯一的 canonical source set。
6. spec/design 與 receipt source bytes 完全相同；todo 只可有 checkbox-equivalent 差異。
7. 沒有 active 或 prior verify job；candidate 在 recovery 前後 SHA 完全相同。
8. Manager 判斷與再驗證 eligibility；adapter 只提供 selector；Registry 不查外部來源、不作資格判讀。
9. 單次 CAS 比對 exact old run/source/candidate/stop tuple 並完整保存 new authority/revision/recovery state。
10. 任何負例或 CAS/read-back failure 均無 baseline/pass/phase 部分更新、無新 verify job、無 builder dispatch；同 request 重入冪等，正常 verify/review/ship gate 不變。

## Tasks

- [ ] **T0 dependency, freshness, ownership audit**：在 #1045 blocker 解除前不 intake。開始 implementation 前重新讀 live #1042/#1045/#1046/#562、#1045 merge/head 與 exact receipt contract；映射 WorkAuthority resolver、receipt read-back、manager job admission、run revision/CAS 與目前唯一 state writer。沒有同 run-bound ready receipt/per-file source proof 就停止；不替舊 #961 run 補證據。
- [ ] **T1 tests / RED：structured stop**：鎖定 verify dispatch 前唯一 planning drift 類型產生 versioned stop code/context、stop 綁 exact run/candidate/source、zero verify-job creation。加入同義/截斷錯誤文字但 code 不同、一般 ValueError、#562 planning-lane failure、已有 verify job 等不可誤判負例。
- [ ] **T2 source / Manager stop**：在 Manager verify dispatch 邊界將明確的 planning-input drift 轉成 typed/versioned stop evidence；不要攔截所有 ValueError，不進 #562 classifier；持久化診斷後阻止 verify job 建立。
- [ ] **T3 tests / RED：receipt-bound eligibility**：以 #1045 的真實 receipt schema 建立正例；缺 receipt、hash/ready/run/work/card/revision/ref 不符、缺/重/未知/跨 revision/unmerged/unsafe source、operator/builder source、spec/design 變更、todo 任一非 checkbox-only 變更、extra artifact 和 active/prior verify job 均斷言無 rebind/dispatch。
- [ ] **T4 Manager / trusted source revalidation**：Manager 載入 receipt、重驗 canonical hash/binding/source rows，重取一份 WorkAuthority snapshot；逐檔確認 spec/design 原文與 hash 精確相同，todo checkbox normalization 後相同，candidate inputs 符合同一規則。來源 resolver 僅回傳材料、不裁定 eligibility；此工作依賴 #1045 已交付的 API。
- [ ] **T5 tests + request adapter**：recovery action 必須要求 exact expected_run_id 和 expected_candidate，只傳 selector/既有 envelope；缺 selector、錯格式或夾帶 receipt ref/hash、baseline、pass/evidence 等 caller proof 在寫 request 前拒絕。不要擴張 recover-planning 或一般 retry-verify。
- [ ] **T6 restricted Registry CAS**：Manager 把剛才 revalidated 的完整 source tuple 和 expected-old run/candidate/stop tuple 傳入專用 CAS；Registry 僅比較/保存本地欄位。測試 CAS race、generation/source/candidate/stop 改變及持久化衝突都零部分欄位更新。
- [ ] **T7 Manager read-back / normal verify / idempotency**：受控 admission boundary 下原子 rebind 到 receipt-equivalent source，維持 candidate，read-back 精確相符後才放回標準 verify dispatcher；不可重派 builder或造 pass。測試 CAS 後 crash、重入、dispatch response 不明、並行 job race，確保最多建立一筆 verify job並保留原 verify/review/ship gates。
- [ ] **T8 documentation / CLI / policy delivery**：更新必要 README/help/生命週期文件、唯一 work item、CHANGELOG [Unreleased] 和 changelog.d/verify-planning-drift-recovery.md；完成當時要求的 PR-context policy、OpenSpec、CLI help、focused/full tests、diff 與 exact-head CI/review。此規劃 PR 未修改 runtime，不把本次規劃 gates 當產品測試/部署證據。
- [ ] **T9 delivery accounting**：只有在 #1045 交付、#1046 triad accepted 且實際 sizing 不再 Red 後才進正式 intake；分開記錄測試、產品 CI、review、merge 和 loaded runtime。Red split 未完成前不派 builder，不稱已恢復 #961。

## Red split — child issues created; no intake

Draft helper 實算 10/Red，accepted-only projection 仍 8/Red。#1046 的全部驗收保留，已查重並建立下列 issue-backed 子票。它們各自必須建立 accepted triad、重新執行 repo sizing；依賴未交付前不能 intake：

1. [#1058 verify drift structured stop](https://github.com/hamanpaul/paulsha-cortex/issues/1058)：只建立 verify-dispatch planning input drift 的 exact run/candidate/phase/source structured stop。與 #562 planning-lane taxonomy 分開；不做 rebind、不改 candidate 或 Registry。可獨立於 #1045。
2. [#1059 receipt-bound eligibility](https://github.com/hamanpaul/paulsha-cortex/issues/1059)：依賴 #1045 的 final ready-receipt/source contract 和 #1058；Manager 唯讀驗證 exact run/candidate、stop、來源、no verify job、spec/design 精確相同及 todo checkbox-equivalent。不能寫 run 或派 verify。
3. [#1060 candidate-preserving CAS and verify redispatch](https://github.com/hamanpaul/paulsha-cortex/issues/1060)：依賴 #1045、#1058、#1059；加入 selector-only action、Registry 限定 CAS、exact read-back、冪等與 crash/job-race handling，原 candidate 交回標準 verify。不得重派 builder或改正常 gates。

#1045 receipt field/path/source API 的實際名稱由 #1045 final contract 決定，本票及 children 不猜測。任何 child 若仍 Red，須再 issue-backed 拆分。拆出 issues 不代表接受規劃或取得 Cortex intake authority。

## Completion boundary

此 Todo 僅供審查規劃。依賴 #1045 未完成且 sizing Red 時維持 Draft；不得進 Cortex intake、不得派 builder、不得改 runtime。父票 #1042 的整體完成與 #961 歷史 run 另有獨立驗收。
