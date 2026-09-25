---
status: draft
work_item: verify-planning-drift-recovery
---

# Verify planning-drift recovery 規格（#1046）

## Requirements

### Authority and dependency

本規格直接對應 [#1046](https://github.com/hamanpaul/paulsha-cortex/issues/1046)，承接父票 [#1042](https://github.com/hamanpaul/paulsha-cortex/issues/1042) 的候選保留切片，並明確依賴 [#1045](https://github.com/hamanpaul/paulsha-cortex/issues/1045) 交付逐檔可信來源、明確 ready review receipt 與 receipt-bound planning baseline CAS。基準日 2026-09-25，#1045 仍為 OPEN；Draft PR #1048 尚未交付其依賴契約。因此本三件套維持 draft，不能 intake 或派實作。

#1046 不追認已 abandoned 的 #961 run。沒有 #1045 receipt 的歷史 run，包括 #961，都不得以 phase、candidate、plan_review_passed、一般 gate evidence、錯誤文字或本機文件補造審查證明；只能由正式新 run 接續。

### R1. Verify 派工前留下結構化 drift stop

當 Manager 在建立 verify job 前偵測 planning input 與 frozen authority 不符時，必須記錄可機讀的 stop code（建議穩定值 verify-planning-input-drift）與完整上下文。stop 至少綁定 schema/version、repo、work_id、claim/work card、exact run_id、verify phase、exact candidate SHA、舊 planning source revision、每份 canonical spec/design/todo ref 與 kind、frozen/current content hash、WorkAuthority/source revision，以及 verify job 尚未建立的 job 狀態。需要人讀的說明可並存，但不能拿被截斷的英文 reason 反向判斷恢復資格。

此 stop 與 [#562](https://github.com/hamanpaul/paulsha-cortex/issues/562) 的 planning lane failure taxonomy 分界如下：#562 處理 planning runtime／BrainstormResult 的結構化失敗分類；本票只處理已產生 candidate 後、verify dispatch 前 _workflow_input_snapshot 的 planning drift。不得把本 stop 分類成 planning-runtime environment/content failure，也不得沿用 define 專用的 recover-planning 來取得恢復資格。一般 verify failure 的 retry-verify 仍按既有契約處理。

### R2. 只以 #1045 durable receipt 證明審查及來源

Manager 恢復前必須讀取並驗證 #1045 的 immutable ready plan-review receipt。Receipt 必須可由其 content hash 重驗，並依 #1045 最終契約精確綁定同一 run、repo、work_id、claim/card、明確完成且 ready=True 的 review result、完整 canonical artifact set、每檔 WorkAuthority source revision/blob-or-content hash，以及 review 實際讀取的 bytes。Recovery 不得自行創建或改寫 plan-review receipt。

Manager 必須從一份新的受信任 WorkAuthority snapshot 重新解析完整 canonical planning source set，並建立從舊 run baseline、同 run 的 #1045 receipt 到目前受信任 source 的完整連結。spec 與 design 必須和 receipt 所證明的來源 bytes 完全相同；todo 只可在 checkbox 標記正規化後相同，其他文字、順序、段落或額外 artifact 的變動一律拒絕。候選樹中的規劃檔也必須符合相同比較規則。來源必須是 WorkAuthority 所指向的 canonical source；operator worktree、builder candidate、本機同名文件、未合併來源或 caller-supplied ref/hash 均不能作為信任根。

### R3. 恢復資格限於 exact run/candidate 與 verify 尚未派工

只有以下條件全部成立才可 rebind：

- WorkAuthority 對 repo/work_id/issue binding 唯一且仍有效；WorkflowRun 是該 work 的唯一 active exact run，且尚未終結。
- request 指定 exact expected run_id 與 expected candidate SHA；兩者逐一等於當前 run 與 Manager 確認的 candidate。
- run 停在 verify/needs_human，附有 R1 的 exact structured stop；stop 的 run、candidate、phase、source revision 與目前 persisted tuple 全部相符。
- 該 run 沒有 active 或任何先前建立的 verify job。此 recovery 只修復 verify dispatch 前的 planning drift；已有 verify job 時走其既有終態/重試契約。
- #1045 receipt 與當前可信來源符合 R2；spec/design 完全相同，todo 僅 checkbox-equivalent，沒有其他內容、來源或 artifact set 漂移。
- Manager 在受控 workflow/job admission 邊界重新驗證上述條件，且沒有並行 transition 或 job 建立。

任一條件未知、不完整、歧義或不符即 fail-closed。不能因 plan_review_passed、phase、一般 gate ledger 或文字錯誤看似成功而放行。

### R4. Manager 判定、adapter 傳 selector、Registry 執行受限 CAS

Recovery 入口只傳 selector 與既有 request envelope：repo、work_id、exact expected run_id、exact expected candidate SHA、actor。不得接收 receipt 路徑/hash、planning bytes、新 baseline、pass flag、evidence ref 或 caller 對 eligibility 的結論。需新增專用 verify-planning-drift recovery action；不得擴張 recover-planning 或讓一般 retry-verify 隱式 rebind。

Manager 是唯一讀取 WorkAuthority、stop evidence、#1045 receipt、規劃來源及 job inventory，並判斷 eligibility 的角色。Registry 不解析 issue、receipt、Git、provider 或 artifact；它只對 Manager 傳入的完整 expected-old tuple 執行一次受限 CAS。CAS 至少比較 run/work/repo/claim、phase/status/facets、generation/revision、candidate、structured-stop ref/hash、完整舊 planning authority/source revision。成功時原子保存完整 revalidated planning authority/source revision 與 verify recovery/reset 欄位；candidate 不變，不建立 builder dispatch，不寫 verify pass 或偽造 gate evidence。若無法將必要欄位放在同一 Registry transition，或不能以 Manager admission boundary 排除 verify job race，先停止並重新設計，不得做分段寫入。

Manager 必須 exact read-back 新 tuple 後才讓既有 verify dispatcher 對原 candidate 派工。後續照原 verify、review、ship gates 運作；rebind 不是驗證成功或交付完成的證據。

### R5. 重入與失敗必須安全

同一 run/candidate/receipt/source 的相同 request 重入必須是冪等結果；不可建立第二筆 verify job、第二次 baseline transition 或改派 builder。若 CAS 已提交而呼叫端在 read-back／dispatch 回應前中斷，重入需從 persisted exact tuple 判明狀態，仍由正常 dispatcher 恢復；不能用舊 snapshot 再寫一次。

缺 receipt、receipt 非 ready、錯 run/head、舊 generation、wrong phase/stop、active/prior verify job、unknown/duplicate/unmerged/cross-revision source、hash mismatch、unsafe/escaping/symlink ref、spec/design 改動、todo 非 checkbox-only 改動、額外 artifact、CAS 競爭、read-back mismatch 或重入內容不一致，均不得改 baseline/pass/phase 或建立 verify job，並保留可診斷 stop。CAS/read-back 失敗不得部分更新 planning authority、source revision 或 recovery state。

### R6. 驗收案例保留所有負例

正例必須使用有 #1045 ready receipt 的 active exact run：Manager 重驗完整來源；spec/design 精確相同，todo 只有 checkbox 等價差異；run/candidate/stop 相符且無 verify job；Registry 一次 CAS 成功、exact read-back 後，原 candidate 進入正常 verify。

負例至少覆蓋 R5 全部資格失敗類型，並斷言 recovery 不產生部分 baseline/pass/phase 變動、不建立 verify job、不重派 builder。另需覆蓋同 request 冪等、CAS 已提交後的 crash/re-entry、並行 verify/job race、正確的結構化 stop 與相似自然語言錯誤但 stop code 不同，以及 #562 planning-lane failure 不會被當作本票 stop。

## Non-goals

不重開或恢復已 abandoned 的 #961，不使用 retire-delivered，不手改 Registry/evidence，不處理 #937 builder 自行修改 tasks、#897 builder 改寫 pinned 文件，不改一般 verify/review/ship gate，也不修改 #562 的 planning runtime taxonomy。#1045 receipt schema 與 baseline freeze 本身不在本票重做。

## Completion boundary

本三件套只規劃 #1046；不能作為 intake/build authority。#1045 依賴契約尚未完成且本票目前預期 Red，必須維持 Draft 並走 issue-backed split。產品實作、測試、CI、merge 與 runtime 分別取證。
