---
status: draft
work_item: existing-pr-journal-authority-readback
issue: 1070
parent: 1055
---

# 既有 Candidate／PR 的 delivery journal authority read-back 規格（#1070）

## Requirements

### R1 — 與 #1069 的唯讀交接

#1069 負責 operator 明示 resume、同一 run 的 eligibility、fresh Todo WorkAuthority、#1068 exact registry CAS，以及同一 Candidate 的 verify/review。依 root owner handoff，#1069 只提供既有 journal row identity 與 durable revision 的唯讀快照，不更新 journal。#1070 消費 #1069 完成的同 run/reverify 結果，獨立負責已有 Candidate／PR 的 journal authority provenance read-back 與必要的 exact-row conditional update。#1070 不重做 reset eligibility、authority resolver 或 registry transition。

### R2 — 單一既有 run／Candidate／PR 綁定

所有 journal 和遠端核對都必須綁定同一組 run_id、repository、work_id、claim era、Candidate SHA、既有 PR number 與 #983 journal row identity。經認證的 GitHub read 必須確認原 PR 仍屬原 repository、狀態為 OPEN，且 head 等於原 Candidate。不得搜尋、建立、替換、重新綁定其他 PR，也不得計算新 Candidate。

### R3 — 消費 #983 conditional-write 契約

只可消費已合併到 main 的 #983 conditional-write／serialization API，使用其 exact durable journal revision、row identity 與 canonical payload hash。若目前 API 無法在 exact row identity 下安全讀回或更新該 row，停止並修訂 #1070；不得複製 writer、擴大 #983 API、增加另一個 store 或引入 registry primitive。

若 exact new-authority row/result 已持久存在，回傳同一 durable result；不得新增 row/event、改 timestamp、重做 PR 操作或重寫既有 transaction result。若需要同步 authority，只可在同一既有 row 上 conditional update 經 #1054 source gates 驗證的 current authority digest/vector，並完整保留 run、Candidate、PR、row identity 與 transaction result。

### R4 — 精確重播、衝突與 unknown

相同 journal transaction identity 與 canonical payload digest 的重播必須是 idempotent no-op。相同 identity 的 payload/digest 不同、stale journal revision、缺漏或衝突 identity、writer conflict，均回 typed stop。持久化結果為 unknown 時，只能以同一 identity conditional read-back；若仍無法確認，必須停止。不得把可見檔案或 exception 當成 durable confirmation。

### R5 — Crash/re-entry 不產生重複事件

row load、conditional update、durable confirmation 與 fresh read-back 前後的 crash/re-entry，都只能觀察原 row 或唯一一次已確認的更新。fresh process 不得產生 duplicate journal event、時間戳或 transaction result。

### R6 — 唯讀遠端確認與副作用邊界

這個切片只讀取 authenticated PR facts 並對帳本地既有 row；不得 push、create/update PR、merge、close、聲稱 remote closure，亦不得建立 Candidate C-to-D update intent、執行 exact-D CI 或 closeout。journal read-back 本身不授權後續 merge。

### R7 — Fixture 與 production scope

fixture 使用 run workflow-52d048b72adbd5cae06f、Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc、PR #1049；不得操作 live run、PR 或 journal。fixture 必須保留 #1049 OPEN/DIRTY 時 fail-closed 的結果，並證明 push、PR create/update、merge、close spies 均為零。production diff 只可修改 paulsha_cortex/coordinator/work_actions.py；若需要改 #983 API、schema、github_delivery.py 或第二個 production module，停止並 issue-backed re-scope。

## Acceptance

- [ ] 只在 #1069 已完成 same-run gate 後接續；#1069 的 journal 輸出保持唯讀 identity/revision。
- [ ] exact original PR/repository/head 與 exact durable row/revision/payload 均相符時，讀回原 durable result 或條件更新同一 row 的 authority digest/vector。
- [ ] 已存在相同 authority/result 時為 no-op；相同 identity 改 payload、stale/conflicting row、unknown write 均 typed stop。
- [ ] fresh-process crash/re-entry 不新增 row/event/time/result；unknown 只用同一 transaction identity read-back。
- [ ] #1049 fixture 維持 OPEN/DIRTY fail-closed，沒有推進 merge，也沒有任何 remote write side effect。
- [ ] implementation 不加入 writer/store/registry primitive、PR mutation 或 #1069 reset/recovery eligibility。
- [ ] 所有 production 變更限於 work_actions.py；跨出此界線需 issue-backed re-scope。

## Dependencies and delivery boundary

#983 的 conditional writer 與 exact durable read-back 必須先 landed；#1069 的 same-run path、#1068 exact registry CAS，以及 #1054/#1063/#1064/#1065 的 Todo/source/freshness/digest gates 也必須先 landed。#1069 與 #1070 對 work_actions.py 變更須序列化，並在正式 intake 前指派不同於 #1068/#1069 的 delivery-journal integration maintainer。

截至 2026-09-25 的 live planning snapshot，#983、#966、#1054、#1063、#1064、#1065、#1068、#1069 與 #1055 均 OPEN；PR #1049 為 OPEN/DIRTY。這是 intake blocker 清單，不是產品實作結果。本規劃包不執行 intake、不操作 live run/PR、不交付產品 code、merge 或部署。
