---
status: draft
work_item: planning-review-source-receipt
---

# Plan review source receipt 規格（#1045）

## Authority and boundary

本規格直接對應 [#1045](https://github.com/hamanpaul/paulsha-cortex/issues/1045)，承接父票 [#1042](https://github.com/hamanpaul/paulsha-cortex/issues/1042) 的「plan review 後凍結新 baseline」切片。#1046 的 exact run/candidate verify 恢復明確依賴本票，留在另一 work item；本票不追認已 abandoned 的 #961 run，也不恢復其 candidate。

唯一決策者是 Manager：它解析受信任 WorkAuthority source、執行 plan review、簽發 ready receipt，並提出 baseline transition。Registry 只執行精確、受限的 compare-and-swap，不讀 issue、worktree 或 review artifact 來自行判斷 eligibility。

本票不處理 #937 builder 重排 tasks、#897 builder 改寫 pinned 文件、一般 verify gate 放寬、既有 candidate recovery、手動改 registry/evidence，或產品 runtime 的其他治理流程。

## Requirements

### R1. 單一 WorkAuthority snapshot 的逐檔來源證明

Manager 在 plan review 前取得一份完整且受信任的 WorkAuthority snapshot，並以該 snapshot 的單一 authority digest、repo、work_id、provider identity/revision 及完整 source_revisions 作為此輪來源上下文。WorkflowRun 的 source_revision 是 WorkAuthority digest，不是 Git commit；不得將兩種 revision 混用。

每個 canonical planning artifact 都要有來源列，至少包含：

- 唯一 canonical repo-relative ref、kind、work_id。
- 同一份 WorkAuthority snapshot 的 authority digest 與 provider/source revision。
- 可由該 revision 解析的 Git blob object ID；若 provider 沒有 Git object database，則須提供可重驗的 immutable revision identity 和精確內容 SHA-256。不能只讀工作目錄後把其 hash 當成來源證明。
- Manager 實際交給 plan review 的 bytes SHA-256。review bytes、來源 bytes 和準備凍結的 baseline bytes 必須完全相同。

每列必須在同一 WorkAuthority snapshot 下解析，且整個集合必須唯一、完整，符合本 run 的 repo/work_id 及 planner 宣告的 canonical output。缺少／重複／未知 ref、kind 或 work_id 不符、unmerged source、來源跨 revision、blob/hash 不符、UTF-8 無法讀取、symlink、absolute/parent traversal、離開 repo root 或任何無法證明的來源都拒絕。不得用 operator worktree、builder candidate 或另一份同名文件補證。

### R2. ReadyPlanReviewReceipt 是明確且不可變的證據

只有最後一張 pending plan card 的 review 明確完成，且 Manager 收到 plan review outcome 的 ready=true，才可以簽發 receipt。receipt 至少綁定：

- schema/kind、run_id、repo、work_id、claim_key、plan phase/card。
- 本次 WorkAuthority digest、provider identity/revision、完整 source_revisions。
- 明確的 review result：ready=true、failed_check=null、terminal=false、checks_run，以及正規化 observations 的 digest。
- 每一份 canonical artifact 的 R1 來源列，並以排序後的 ref/kind/work_id/source identity/content hash 固定完整集合。

receipt 採 canonical serialization，以內容 SHA-256 命名並只建立一次；重入時只可逐 byte 重用相同內容。已存在但內容不同、receipt/ref/hash 不匹配或路徑不安全時拒絕。receipt 寫入成功但 registry CAS 未成功時，未被 WorkflowRun 引用的孤兒 receipt 不具授權效果，可供安全重試或留待後續清理。

plan_review_passed 布林值、phase、普通 gate ledger、candidate、planner 自述或既有 evidence 不能推導或補造 receipt。歷史 run 沒有 receipt 就視為沒有可信 review binding。

### R3. Receipt 與 planning baseline 由單一受限 transition 凍結

Manager 將 receipt durable 寫入並 read-back 驗證後，Registry 只在 exact run/work/repo/claim、plan phase、最後 pending card、expected old planning_authority/source_revision、run status/generation 仍符合時接受 transition。transition 一次寫入：

- receipt 的 canonical ref/hash。
- 完整的新 planning_authority（涵蓋全部 canonical artifacts）。
- 對應的 planning_source_revision。
- plan_review_passed=true。
- plan step 的完成狀態、phase/attempts 前進資料。

Registry 不可接受部分欄位更新，也不可以單獨把 plan_review_passed 設 true。CAS conflict、任何 source revalidation drift、receipt read-back mismatch 或 persisted state 不同都必須零 baseline/pass/phase 更新、零 build dispatch，並留下可診斷 stop。Manager 在 Registry commit 並 exact read-back 後才可派 build；不得因 receipt file 已寫入或 CAS 呼叫已送出就派工。

同一個 receipt 的 exact replay 是冪等 no-op；run、source、review、舊 baseline 或新 baseline 任一欄不一致的 replay 必須拒絕。Registry 的責任止於比較其本地 expected/current run tuple 並原子保存完整 transition，不自行查 GitHub、source provider 或 artifact bytes。

### R4. 未完成 plan review 永不 rebind

下列情境均不得簽 receipt、更新 planning_authority/planning_source_revision、設定新的 plan_review_passed，或因本票跨入 build：

- Green sizing band 或沒有 sizing band：維持既有無需 plan review 的路徑；沒有 drift 時保留原行為且不鑄造 review pass。若需要 rebind 才能繼續，fail closed。
- Yellow sizing 下 gate 回傳 None：維持舊 fail-soft 行為，不鑄造 ready outcome 或 receipt；若需要 rebind 才能繼續，fail closed。
- Yellow review 明確 rejection、non-terminal failure、terminal failure、輸入不完整或 source proof 不完整。
- review 後任一 spec/design/todo bytes 改動、額外內容變更、來源/authority revision 改變、CAS 競爭或重複/衝突來源列。

Yellow 且明確 ready=true 時，合法的 accepted planning text 修正可被 rebind，但 receipt 和 baseline 必須指向 review 實際看到的同一批 bytes。

### R5. 驗收案例

- 正例：#1042/#961 的 T6 合法文字修正由可信來源提供；最後 plan review 對該 exact todo bytes 回 ready=true；receipt 逐檔記錄來源，Registry 一次提交新 baseline 和 receipt，read-back 完全相符後才派 build。
- 負例：review 前後的額外 spec/design/todo 文字變更、錯 kind/ref/work_id、unknown/unmerged/cross-revision source、hash mismatch、unsafe ref、duplicate ref、缺檔、review rejection/nonterminal/terminal、Green、no band、Yellow gate None、CAS race、receipt 衝突；所有負例均無 baseline/pass/phase 部分更新且不派 build。
- 保留無 drift 的既有 Green/no-band 路徑；它們即使可前進也不新增 plan review receipt/pass。

## Acceptance boundary

本規格只授權 #1045 的來源證明、ready review receipt 與原子 baseline freeze。它不授權保留或恢復既有 candidate；候選恢復與結構化 verify drift stop 由 #1046 另行規劃及驗收。
