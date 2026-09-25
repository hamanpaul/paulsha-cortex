---
status: draft
work_item: existing-pr-journal-authority-readback
issue: 1070
parent: 1055
---

# 既有 Candidate／PR 的 delivery journal authority read-back 設計（#1070）

## Context

#1055 將已有 Candidate／PR 的 Todo authority recovery 拆成 #1068 registry transition、#1069 same-run Manager recovery 與本票 delivery-journal reconciliation。#1069 的責任是讀取唯一 current authority、以 #1068 CAS 重啟同 run、重跑 verify/review；依 root owner handoff，#1069 對 journal 只讀取 exact identity/revision，journal row 的條件更新與 durable read-back 唯一由 #1070 負責。

#983 是共用 delivery-journal conditional writer 的 owner。#1070 只能在其 API landed 後呼叫既有能力；不擴充 API、不直接寫檔、不引入 registry CAS。#983 fixture run/Candidate/PR identity 僅用於 stub fixtures。

## Goals / Non-Goals

**Goals:**

- 在 #1069 同 run verify/review 完成後，核對同一 journal row、current authority digest/vector 與原 Candidate／PR。
- 對已確認的新 authority/result 做 durable read-back；必要時只以 #983 CAS 更新既有 row。
- crash/re-entry、conflict 與 unknown 均保留 exact transaction identity，且不產生重複 journal event 或遠端副作用。

**Non-Goals:**

- 不做 Manager restart/resume eligibility、authority sourcing、#1068 registry transition 或 verify/review dispatch。
- 不加入 registry primitive、通用 journal writer、schema、GitHub API 或第二個 production module。
- 不做 PR push/create/update/merge/close、Candidate 重算、C-to-D intent、exact-D gates 或 closeout。

## Decisions

### D1 — 固定單向責任交接

#1069 對 journal 的介面只讀，回傳其核對過的 row identity 與 durable revision，不能提交 journal mutation。#1070 接收 #1069 已完成的 same-run gate 結果與 #1054 已驗證的 current authority digest/vector，獨立完成既有 Candidate／PR 的 journal reconciliation。#1070 不重判 reset eligibility，也不擁有 generic restart recovery。

### D2 — 先核對相同遠端物件與 durable row

在變更既有 row 前，重新 authenticated-read 原 repository/PR，要求原 PR number、OPEN 狀態和 head==Candidate。對 journal 使用 #983 API fresh-load 並比較 exact revision、row identity、run/PR/Candidate tuple 與 canonical payload hash。沒有唯一 exact match、PR 已關閉、head 漂移或遠端讀取 unknown 時，回 typed stop；不得以另一個 PR 或 head 自動代換。

### D3 — 只允許同一 row 的 authority provenance 同步

若 exact row 已含 current authority digest/vector 和 durable result，回讀原 result 並 no-op。若 row 仍綁定 old authority，只有在 #983 已發布 API 明確支援 exact-row conditional update 時，才以 current durable revision 更新 authority digest/vector。保留原 run、repo/work/claim era、Candidate、PR、transaction identity、已有 result 與其他 row；不 append 新 row/event、不修改結果時間。若 API 只能 append event、不能精確更新既有 row，停止並回 #1070 重定 scope。

### D4 — 同 identity conditional recovery

conditional update 回 committed 後做 fresh exact read-back，核對 revision、row identity 與 current authority payload，再回傳 durable result。若寫入回 conflict，回 typed conflict；若 outcome unknown，僅用同一 identity 和 canonical payload作 conditional read-back，找到完全相同的更新才視為已提交，其他情況均停止。重播不呼叫 push/create PR/merge/close。

### D5 — Crash/re-entry 保留唯一 transaction

每個 crash point 都重進同一 transaction identity。已存在的同 payload result 是 durable no-op；不存在且 baseline still exact 時可走既有 conditional attempt；同 identity 不同 payload、stale revision 或 row identity drift 均為 typed stop。不可由 in-memory run 或檔案可見性補造成功結果。

### D6 — 真實 PR 對帳與 fixture 隔離

fixture 對應 workflow-52d048b72adbd5cae06f、Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc、PR #1049。所有 GitHub 呼叫都 stub；PR #1049 的 OPEN/DIRTY 狀態維持 fail-closed，journal read-back 不會把它推到 merge。production push/PR-create/update/merge/close spies 必須全為零；live PR 僅作為記錄的遠端狀態，不對它執行測試。

## Risks / Trade-offs

- #983 API 若不支援 existing-row conditional update，需求不能安全落地；回 issue 修訂，不能複製或擴大 writer。
- PR facts 在 journal commit 前漂移會使結果停止；保留 existing row，交由既有 gates 處理。
- unknown 在 fresh read-back 仍不確定時會停住；這保留唯一 row/transaction identity，避免製造重複結果。
