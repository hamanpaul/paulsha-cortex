---
status: draft
work_item: same-run-authority-restart
---

# 既有 Candidate run 的 same-run authority restart 規格（#1069）

## Requirements

本 work item 唯一 owner 是 [hamanpaul/paulsha-cortex#1069](https://github.com/hamanpaul/paulsha-cortex/issues/1069)，唯一 `work_id` 是 `same-run-authority-restart`，唯一 OpenSpec change 是 `same-run-authority-restart`。正式來源綁定由 `.cortex/work-items.yaml` 明確登錄 issue、OpenSpec change、本 spec、design 與 Todo。父項為 [#1055](https://github.com/hamanpaul/paulsha-cortex/issues/1055)。

本規劃的硬前置為：#1054 及其 #1063/#1064/#1065 accepted contract 必須先合併；#1068 必須在 #966 exact-revision JobRegistry CAS 合併後完成並發布其 WorkflowRun transition API。這些前置未滿足時不得開始 product intake 或 implementation。#1070 是後續 PR/journal reconciliation child；#983 conditional writer 由 #1070 消費。#1069 不提供這些 primitives。

1. **R1 — 只有明確 operator resume 可觸發**：Manager 只在 operator 對同一 `repo`、`work_id` 明確呼叫既有 `work resume` 後，才評估此 recovery。periodic scan、automatic claim、start/intake 與背景 tick 不得 reset run、改 claim era 或派送 recovery job。run 不唯一、request 不完整或 expected tuple 已變時回 typed fail-closed。
2. **R2 — 重新取得唯一、fresh 的 WorkAuthority**：使用 #1054 發布並由 #1063/#1064/#1065 保證的 canonical Todo qualification、strict trusted WorkAuthority reader 與 successful Monitor generation。Todo 必須由唯一 owner 發布，具有同一 issue/work-item provenance、matching `work_item` metadata 與具體 Tasks。保存 old/new sorted `source_revisions`、`snapshot_hash`、`provider_revision` 及完整 `work_authority_digest`；source revision 必須來自已驗證來源。Todo link override、plan path 相似度或 registry sequence 不是 source authority。缺失、重複、過期、refresh 失敗或 publication 後 digest drift 都停止。
3. **R3 — 同一個 eligible Candidate run**：精確匹配唯一的既有 `run_id`、repo、work_id、status、verify/review phase、retry classification、舊 `claim_key` 與 `source_revision`。Candidate 已存在且其 SHA 固定；必須讀回 verified head、gate/evidence refs、job refs、PR refs、active job snapshot。只接受同一 run 的 ongoing verify/review recovery 狀態；不得接管 superseded、merged/closed PR、無 Candidate、錯 claim era、錯 work/repo、另有 active job 或多筆相符 run。
4. **R4 — Reset 前讀回完整 tuple**：捕捉 durable registry revision 及 R3 tuple，並唯讀核對既有 delivery-journal row 的 exact identity/revision；透過 authenticated GitHub read 核對唯一 open PR 的 repository、PR identity、head 等於 Candidate SHA 及 current state。缺欄位、未知或衝突事實都 fail closed。此處 journal 操作限 read；不寫 journal、不進行 PR 或其他 delivery mutation。#1070 負責完整 journal authority read-back/conditional delivery。
5. **R5 — 單次 exact CAS**：在任何 reset 或 dispatch 前，以 #1068 已發布 API 呼叫一次 exact WorkflowRun transition，提供 expected durable registry revision 和完整 expected tuple，以及已驗證的新 authority digest/source binding。成功才進入 post-CAS 檢查；CAS conflict、重複 request 不同 payload、任何 tuple drift 或 persistence unknown 都 typed fail closed，不得直接呼叫 registry writer或用 caller patch 改寫 run。
6. **R6 — Dispatch 前再次驗新 authority 與相同交付物**：CAS 成功後重新取得 successful/fresh WorkAuthority，重新比對 digest、sorted source revisions、snapshot/provider revisions、同一 run、新 claim era、同一 Candidate、同一 open PR/head/state 及 exact no-active-job snapshot。所有比對通過後才重跑 verify/review。build 不重跑、不建替代 run、不改 Candidate 或 PR head。新 jobs/evidence 必須綁相同 run_id/repo/work_id/Candidate SHA 與新 claim era；重用舊 claim bindings 或舊 verify/review evidence 不可冒充新 gate。
7. **R7 — 保留不可變歷史**：僅由 #1068 transition invalidate verify/review gates 並建立新 claim/source era。保留 Candidate、build/repair steps、builder jobs/evidence、舊 JobRegistry rows、舊 claim bindings、舊 evidence refs/bytes、PR refs 與 run identity；不改寫或刪除舊值。任何失敗都保留 Candidate、PR、journal 及 history。
8. **R8 — Fixture-only 行為證據**：以真 Manager/WorkAuthority fixtures、stub GitHub reads 與 #983 fixture tuple（run `workflow-52d048b72adbd5cae06f`、Candidate `7ba7e877c94ff4eee72ba796ea9f8962953ed5cc`、PR #1049）驗合法 unique/fresh Todo 路徑及 missing/ambiguous/stale Todo、publication 後 digest drift、wrong run/claim/Candidate/PR tuple、active job、舊 evidence stale、concurrent resume、CAS conflict、CAS 前後 crash、repeated request。所有 GitHub/delivery write spies 必須為零。這些只證明 fixture；不得操作正式 #983 run 或 PR #1049，也不得把 fixture 結果稱為 live provider evidence。
9. **R9 — 範圍與交付邊界**：production code 僅 `work_actions.py`；允許新增對應測試、docs、changelog 與規劃 artifacts。不得新增 Todo/source resolver、path scanner、parser、registry primitive、registry writer 或 delivery-journal writer；不得 push/create/update/merge/close PR 或執行 delivery mutation；不得執行 abandon、recover-pre-candidate、supersede、retire、替代 run。若 #1068/#1054 已發布介面不足，或需第二個 production module / cross-writer API，停止並更新 issue-backed scope/dependency，不得在本票擴權。

## Boundary

本票由 Manager/work-actions owner 負責 explicit resume 的 authority decision 與 reset 前後順序。#1054 負責 first-Builder admission；#1068 負責 atomic exact-CAS transition；#1070 負責同一既有 PR 的完整 delivery-journal reconciliation；#983 負責它所需的 conditional journal writer。#962/#975/#976/#977 是 merged PR closure；#1015/#1017 是 Candidate C-to-D、PR update、push、CI 與 closeout；#972/#973 處理 PR #1049 main conflict。上述責任不併入本票。

規劃 PR merge 只發布 planning authority 與唯一 mapping，不代表實作、intake、dispatch、#983 run/PR 狀態或交付完成。實作 intake 須在全部硬前置 landed 且 root review 接受完整 planning bundle 後另行執行。
