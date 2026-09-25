---
status: draft
work_item: planning-review-source-receipt
domain_breadth: 2
state_consistency: 2
invariant_count: 9
artifact_classes:
  - source
  - tests
  - documentation
---

# Plan review source receipt Todo（#1045）

## Boundary

- 唯一 work item：planning-review-source-receipt；直接 issue 為 [#1045](https://github.com/hamanpaul/paulsha-cortex/issues/1045)，父票 #1042；規格及設計分別見 [spec](../../specs/planning-review-source-receipt-spec.md)、[design](../../specs/planning-review-source-receipt-design.md)。
- #1045 僅負責逐檔可信來源證明、明確 ready receipt 和 plan review 後的完整 baseline 原子凍結。#1046 的 exact run/candidate verify recovery 依賴此 receipt，另票處理；#937/#897 另有來源與 owner。
- 唯一判定 owner 是 Manager；Registry 僅做受限 CAS。預估 production modules 為 manager.py、work_bridge.py（唯讀 source snapshot resolver，若現有接口不足）及 registry.py（受限 transition）。不得讓 WorkBridge 或 Registry 代替 Manager 判斷 review eligibility。
- 規劃基準：2026-09-25 live #1045/#1042/#1046、Draft PR #1044。PR #1044 仍是 OPEN/Draft、head d0a548595974f0c1c736baf3a2904fc28156071c；其 #1042 triad 不納入本 work item，也不修改其 worktree。此次 isolated worktree 起點為 origin/main 729d3421634b34c5bb226350167b884dc3440148。
- 現況核對：PlanningArtifactAuthority 只有 ref/kind/work_id/baseline_sha256；WorkflowRun 有 planning_authority、planning_source_revision、plan_review_passed，但沒有 ready receipt。source_revision 是 WorkAuthority digest，不是 Git commit。現行 Manager 僅對 Yellow 最後 plan card 呼叫 plan review；Green/no-band 不呼叫，Yellow gate None fail-soft 並不設定 pass。現行 registry update 可一次保存多欄，沒有此切片需要的 expected-old tuple CAS。

## Five-dimension sizing

| Dimension | Draft score | Basis |
|---|---:|---|
| domain_breadth | 2 | 預估需 manager.py、work_bridge.py 的唯讀來源解析，以及 registry.py 的受限 CAS，跨三個 production modules。 |
| state_consistency | 2 | immutable receipt evidence 先落檔，再以 exact run tuple CAS 發布 receipt ref、完整 baseline 和 phase/pass；必須處理 orphan、race、read-back 和重入。 |
| acceptance_surfaces | 2 | fix-standard 的 2 gate_spine 加上 R-09/R-16/R-19，signal=5。 |
| spec_stability | 2 | 三件套仍為 draft，尚未取得接受審查。 |
| orchestration | 2 | packaged fix-standard 為 9 cards 且多張有 persona binding。 |
| **Total / band** | **10 / Red** | Repo 的 current_sizing_snapshot() 對本三件 draft 與 fix-standard 實算 (10, red)；Red 不 intake。 |

Accepted projection 以暫存副本只把三份 frontmatter 的 status 改為 accepted，再用同一 helper 實算為 (8, red)；這只是 sizing projection，不是 review/acceptance receipt。兩次結果的其餘 dimensions 都為 2/2/2/2，spec_stability 分別為 draft 2、accepted 0。任何實際模組邊界或 scope 變更均重算，不沿用此分數。

## Invariants

1. Manager 消費的 review bytes、WorkAuthority source bytes 與凍結 bytes 完全相同。
2. 每檔 ref/kind/work_id/source revision 唯一且來自同一份受信任 WorkAuthority snapshot。
3. Git-backed 來源核對 blob object ID；其他 provider 必須有 immutable revision identity 和可重算 content SHA-256。
4. Receipt 明確表示 plan review ready=true，並綁定 exact run/work/card/result/source set。
5. plan_review_passed、phase、gate ledger 或 candidate 不能補造 receipt。
6. Registry 一次提交完整 receipt ref、planning_authority、planning_source_revision、plan pass 和 phase/step/attempts。
7. CAS/source/read-back 任一失敗都零部分更新、零 build dispatch。
8. Green、no-band、Yellow None/rejection/nonterminal 路徑不 rebind、不鑄造 review pass。
9. exact receipt 重入冪等；來源或舊 run tuple 不一致時 fail closed。

## Tasks

- [ ] **T0 source/API audit**：核對 Manager 最後 plan card 實際可取得的 WorkAuthority provider snapshot、每檔 immutable revision/blob/content metadata、owner lock 與 registry state writer。確認是否能從既有 API取得 exact snapshot；缺少來源材料時不得 fallback 到 run.workspace_root。若需第四個 production module、第二個寫入者或新跨 store transaction，停下重畫切片並重新 sizing。
- [ ] **T1 tests / RED：receipt 與 atomicity oracle**：以 fake provider/Registry 建立完整 T6 positive case；先斷言同一批 reviewed bytes 逐檔入 receipt、baseline 全集合與 receipt/pass/phase 同次 CAS、read-back 後才呼叫 builder。先加 wrong revision/hash/ref/kind/work_id、duplicate/unknown/unmerged/unsafe source、CAS race、receipt collision、read-back mismatch 的零寫入/零派工負例。
- [ ] **T2 source snapshot resolution**：Manager 從同一受信任 WorkAuthority snapshot 解析全部 canonical spec/design/todo refs。若 WorkAuthority resolver 留在 work_bridge，該模組只回傳 immutable source metadata/bytes；Manager 驗 repo/work_id/source set，必要時用 Git object database 核對 blob ID，並比對 exact SHA-256 和 path safety。
- [ ] **T3 ready receipt**：只有最後 Yellow plan card 的明確 ready=true outcome 可建立 canonical immutable receipt；記 run/work/repo/claim/card、review result/checks/observations digest、authority/provider revision 及完整逐檔來源列。re-entry 只重用完全相同 payload；歷史 boolean 無法 mint receipt。
- [ ] **T4 restricted Registry transition**：新增只接受 Manager call 的窄 API，精確比較 expected old run/work/repo/claim/phase/card/source/baseline/generation；一次寫入 receipt ref、完整新 planning_authority、planning_source_revision、plan_review_passed、plan step/phase/attempts。CAS 不符回 typed conflict 且不改任一欄。
- [ ] **T5 Manager commit/read-back/dispatch**：receipt immutable write/read-back 後呼叫 Registry CAS；read-back 逐欄相等後才由同一 Manager 流程派 build。CAS conflict 不以舊 source snapshot重試；下次 tick 重新讀 authority、文件與 review input。
- [ ] **T6 boundary regression**：驗證 #1042 T6 合法文字修正正例；spec/design/todo 額外內容變更、review rejection/nonterminal/terminal、Green、no band、Yellow gate None、來源 drift、重複列、unsafe ref、跨 revision 和競爭 CAS 都不得 rebind。另保留無 drift Green/no-band 舊行為，但不設定 pass/receipt。
- [ ] **T7 documentation / delivery gates**：同步更新這份 spec/design/todo、唯一 work item、changelog fragment 與 CHANGELOG [Unreleased]；完成 sizing、YAML/frontmatter、OpenSpec/spec validation、git diff --check 及帶 PR context 的 policy check。產品 pytest、CI、review、PR/merge、installed runtime 各自於後續實作交付記錄；本規劃不宣稱它們已通過。

## Red split proposal — no acceptance or intake

Helper 若確認 accepted projection 仍為 Red，#1045 先保持 draft，不能用降維宣告或刪負例轉 Yellow，也不派 builder。建議 issue owner 先把 #1045 重拆為下列有明確依賴的 bounded children，再各自建立 accepted triad 和重算：

1. **Trusted planning source snapshot**：只建立唯讀 resolver，讓單一 WorkAuthority snapshot 對每份 canonical ref/kind/work_id 產生可驗 blob/content provenance；涵蓋 unknown/duplicate/unmerged/cross-revision/hash/path 負例。不保存 review result、不改 WorkflowRun、不更新 baseline。
2. **Ready plan-review receipt**（依賴 1）：Manager 對 resolver 交付的 exact bytes 執行 Yellow final-card gate，僅明確 ready=true 才寫不可變 receipt；Green/no-band/Yellow None/rejection 不簽 receipt。不改 phase 或 planning baseline。
3. **Receipt-bound baseline CAS**（依賴 1、2）：Registry 只消費 exact receipt ref/hash 與 expected old run tuple；一次保存完整新 baseline、source revision、receipt reference、plan pass 和 phase/step。Manager read-back 完全相等後才派 build。#1046 仍依賴此 child 的完整交付。

這是拆分建議，不代表已建立子 issue、接受任何 child 或具備正式 intake authority。直到拆分和實際 sizing 經 owner 核准前，本 work item 保持 draft。

## Completion boundary

完成 #1045 的 runtime 實作不會完成 #1042/#961/#1046，也不會證明 verify recovery、PR review、CI、merge 或 installed runtime。產品實作、exact-head delivery、部署/載入 runtime 分別取證。
