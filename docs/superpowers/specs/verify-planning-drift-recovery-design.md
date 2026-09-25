---
status: draft
work_item: verify-planning-drift-recovery
---

# Verify planning-drift recovery 設計（#1046）

## Decisions

### D1. 前置與目前阻擋

#1046 只能消費 #1045 完成後的 immutable ready receipt 契約；目前 live #1045 為 OPEN、Draft PR #1048 官方實算 10/Red，接受狀態投影仍 8/Red。#1045 尚未完成 issue-backed split 與其依賴交付，因此本設計先固定責任邊界，不假設未接受的 receipt field/path 或可用 API。進實作前需讀取已交付的 #1045 triad/API，重新核對 #1042/#1046/#562、worktree/head、receipt schema、source resolver 和唯一 state writer。

若 #1045 沒有提供能重驗 receipt hash、逐檔 source identity/bytes、review ready 結果及 run/work/card binding 的唯讀接口，本票停止重新切分；不能改用 plan_review_passed、舊 run 的普通 evidence 或目前 workspace bytes。

### D2. 結構化 stop 與 #562 分界

在 verify job 尚未建立的 dispatch 邊界，由 Manager 將本次 exact planning-input mismatch 寫成有版本的 stop evidence；reason string 只供診斷，不是 authority。Stop evidence 要帶 run/candidate/phase/context 與每檔 old/current digest，並以 evidence ref/hash 綁回 exact WorkflowRun。

#562 的 failure_kind / planning runtime taxonomy 適用於 define/planning lane。此票應為 verify dispatch 的特定 stop code 建立獨立分支，不把 _workflow_input_snapshot 的 ValueError 一概映射為 drift，也不沿用 _classify_planning_failure 的 substring 結果。recover-planning 現行前置條件是 define/needs_human；retry-verify 現行以 candidate 選定一般 verify rerun；兩者都不具本票所需的 receipt-bound source revalidation，故維持各自 contract。

### D3. Trust chain 與最小比較

Eligibility 對同一次判定構成三段鏈：

1. **Persisted run**：讀出 exact active run、frozen planning authority/source revision、candidate 與 structured verify-drift stop。Stop 中所有 run/card/candidate/context 都須與 persisted tuple 相符。
2. **#1045 receipt**：讀取 immutable, content-addressed receipt；重驗 canonical payload/hash、同 run/work/repo/claim/card、ready=true、完整來源列與 review bytes。Receipt 是「哪些來源已受審」的唯一來源證明。
3. **Current trusted source and candidate**：Manager 取新 WorkAuthority snapshot，對照 receipt 的完整 canonical ref/kind/work_id/source set。spec/design bytes 必須逐 byte/hash 一致；todo 只使用既有或新明確指定的 checkbox normalizer 比較，normalization 前後的 exact 原文及 hash 均留在診斷資料。不得因 WorkAuthority digest 變動就忽略逐檔 identity，也不得把 digest 當 Git commit/blob。

純 checkbox 等價規則不得擴到 Markdown heading、文字、空白、段落、清單順序、非 checkbox 行或新增檔案；未知 kind/ref、path traversal、symlink、duplicate、跨 revision、unmerged source、provider 無法重驗或 hash 缺失都拒絕。若 #1045 的 source interface 無法提供此鏈，停止，不 fallback 到 operator checkout/builder tree。

### D4. Action / owner / module boundary

新增一個明示的 verify-planning-drift recovery action，透過既有受控 work-action transport 傳 repo/work_id、expected_run_id、expected_candidate、actor。adapter 與 control contract 只負責要求 selector 格式與拒絕多餘 caller proof/input；不讀來源、不接收 receipt refs、不改 Registry。

預期 production 邊界，需在 T0 按 #1045 已交付接口確認：

- coordinator/manager.py：在 verify dispatch 前產生結構化 stop；做唯一資格判定；重驗 source/receipt/job inventory；執行受控 transition 與 read-back；按原 verify dispatcher 派回同 candidate。
- coordinator/work_actions.py：驗證 exact selector、WorkAuthority 授權、唯一 active run，呼叫 Manager-owned recovery API；回傳可辨識的 no-op/recovered/stop 結果。
- control/contract.py 與 porcelain/recover.py（若此入口需 CLI 暴露）：只規範 action 名、run/candidate selector、help 與傳輸，不得形成第二個決策點。
- coordinator/registry.py：新增最窄的 Manager-only exact-old-tuple CAS；只比較並保存本地 run 欄位，不呼叫 provider/GitHub 或自行判讀 receipt。
- coordinator/work_bridge.py：只在 #1045 提供的可信 WorkAuthority/source resolver 尚未滿足 revalidation 時新增唯讀 adapter；它回傳 snapshot/source bytes，不判定 recovery eligibility、不寫 WorkflowRun。

不得增加新的 production state writer、把 gate/receipt 判讀搬到 Registry、或讓候選工作目錄成為信任根。若實際邊界擴成另一模組或額外 schema/writer，先重新 sizing/split。

### D5. CAS 與 dispatch 次序

Manager 在同一個 admission/ownership boundary 內重新讀取 active run、job inventory、receipt、WorkAuthority 及 artifacts。只有 exact current run、candidate_head、verify stop/no-job 和來源比較都通過，才傳 expected-old tuple 給 Registry。

Registry 只接受 Manager 提供的 expected run tuple；CAS 原子保存完整新 planning authority/source revision 和必要 verify-reset 欄位，保留 candidate SHA，不新增 passed evidence、不改 builder outputs。任何 run revision/generation/phase/facet/candidate/old source tuple 或 stop ref/hash 已變即 conflict，零欄位更新。Registry 沒有 JobStore 權限，故 no-active/prior-job 的唯一判定在 Manager，並由同一 Manager-owned admission lock/serialization guard 防止 CAS 同時另一條 verify dispatch 建 job。

CAS 後 Manager exact read-back 全部欄位；不相等就停止且不派工。成功後交還標準 verify dispatcher 執行。若 process 在 CAS 和 dispatch 間崩潰，persisted tuple 必須讓下一次正常 tick 唯一地續派 verify；重入不得再 CAS 或建立第二筆 job。#1045 receipt 永不修改、重用或替代 verify/review gate evidence。

### D6. 失敗分類與 side effects

錯誤 stop code、普通 ValueError、文字相似錯誤、#562 planning-lane record、plan_review_passed 或普通 gate ledger 都不能成為資格。無法判讀 stop/receipt/source/job 狀態是拒絕，而不是環境 retry。

Recovery 的負例斷言 Registry tuple 完全不變且沒有新增 verify job。receipt 若已存在，拒絕路徑不覆寫、不刪除；R1 stop evidence 保留供診斷。若 CAS 成功但 read-back 失敗，既有 durable state 是唯一依據；不得再送不帶同一 expected tuple 的較寬 transition。若 dispatch response 不確定，由標準 queue/job identity 查重並停止重複建 job。

## Deferred choices

下列值待 #1045 最終 schema 與 T0 source audit 確定，不在 #1045 未交付前猜測：receipt field 名稱/canonical path、source provider revision 欄位、stop evidence namespace、Run generation/revision CAS 欄位、checkbox normalizer 的唯一 import path、恢復 action 的對外 CLI 拼字。這些不得改變 D2–D6 的 owner、selector、CAS 與 negative-test contract。
