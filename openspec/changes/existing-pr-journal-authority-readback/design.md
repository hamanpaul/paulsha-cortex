---
status: draft
work_item: existing-pr-journal-authority-readback
issue: 1070
---

## Context

#1069 的 same-run recovery 驗證 fresh authority、呼叫 #1068 CAS 並重跑 verify/review；它只讀取既有 journal row identity/revision。#1070 接收該 gate 結果後，負責同一 Candidate/PR 的 delivery-journal authority provenance read-back。#983 是唯一 conditional writer owner；其 API 未 landed 前不 intake、不實作。

## Goals / Non-Goals

**Goals:**

- authenticated-read 同一 repository/PR，確認原 PR OPEN 且 head==Candidate。
- 以 #983 exact durable revision/row identity/canonical hash 讀回既有 result，必要時 conditional update 同一 row 的 current authority digest/vector。
- 保留原 transaction result、run/Candidate/PR identity與 timestamp，並提供 same-identity crash/re-entry recovery。

**Non-Goals:**

- 不重做 #1069 reset eligibility、resume policy、WorkAuthority resolver、registry transition、verify/review dispatch。
- 不增加 journal writer/store、registry primitive、schema、GitHub API 或第二 production module。
- 不 push、create/update PR、merge、close、C-to-D、exact-D CI 或 closeout。

## Decisions

### D1 — Read-only #1069 handoff；#1070 conditional delivery owner

#1069 僅核對 journal identity/revision，不寫 journal，也不擁有 existing-row authority synchronization。#1070 在其 same-run gate 結束後接手 journal transaction。此分界避免把 recovery eligibility、registry CAS 與 delivery row mutation合併為同一責任。

### D2 — Exact identity 和 fresh authenticated PR facts

#1070 使用 exact run_id/repo/work_id/claim era/Candidate/PR/row tuple。它再讀一次原 PR 的 authenticated facts，要求 repo/number 不變、state=OPEN、head=Candidate；讀取失敗、merged/closed、head drift、歧義或 tuple mismatch 均 typed stop。不透過 branch 或標題找替代 PR。

### D3 — Consume #983 under exact revision

先用 #983 API 對同一 row fresh-load，再比對 exact durable revision、canonical payload digest、row identity 和 identity tuple。existing exact authority/result 直接回 durable result。只有 API 明確支援時，才 conditional update 原 row authority digest/vector；不得重寫 transaction result/event/time 或其他 row。若 #983 僅提供 append-only event API，issue contract 不足以支持本票；停止並先調整 #1070 scope/dependency，禁止擴造 writer。

### D4 — Unknown requires same-identity read-back

committed 需 #983 回報 durable result 且新鮮讀回完全相符。conflict 立即 typed stop。unknown 只可用原 transaction identity 和 canonical payload conditional-read-back 一次；讀回精確相符才回已持久 result，否則維持 unknown/stop。caller 不因 I/O exception 或 file visibility 推進任何 remote operation。

### D5 — Crash-safe idempotency

row load、conditional update、fsync/durable confirmation 和 read-back 前後的進程重啟，都以同一 transaction identity 執行。相同 identity/digest 是 no-op；相同 identity 不同 payload 或 stale row/revision 是 conflict。任何結果都不得追加 duplicate journal event、改 PR 或自動重試遠端 mutation。

### D6 — Fixture only

run workflow-52d048b72adbd5cae06f、Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc、PR #1049 只進入 stubs。live #1049 OPEN/DIRTY 代表 fixture 必須保留 fail-closed/no-merge 結果。以 spies 斷言所有 push/create/update PR/merge/close 呼叫皆為零。

## Risks / Trade-offs

- #983 API 若不支援 existing-row conditional update，需求不能安全落地；回 issue 修訂，不能複製或擴大 writer。
- PR facts 在 journal commit 前漂移會使結果停止；保留 existing row，交由既有 gates 處理。
- unknown 在 fresh read-back 仍不確定時會停住；這保留唯一 row/transaction identity，避免製造重複結果。
