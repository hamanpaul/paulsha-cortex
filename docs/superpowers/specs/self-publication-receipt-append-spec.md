---
status: accepted
work_item: self-publication-receipt-append
issue: 993
---

# Self-publication receipt registry append 規格（Child B，issue #993）

## Requirements

本票只實作 Child A 定義的 receipt persistence/append seam，production owner 為 registry.py。它必須維持 #978 AC01/AC04，並證明 foreign metadata fixture 經正式 work-action start/intake 路徑仍不成 receipt。

### R1 — Carry forward receipt state

JobRegistry 所有 WorkflowRun reconstruction、copy、ordinary update、serialization、fresh reload 都保存 A 的 exact receipt state，包括 typed rows、opaque invalid rows、invalid-container raw value及 diagnostic。舊資料只有在 key 確實缺席時才讀為 legacy empty。任何普通 create/start/intake/update request 的 receipt 欄位均不得寫入；更新 run 時不得替換/刪除已有 state。Receipt sidecar refs 是相對路徑；本票不得在 registry load 時照 caller 值開檔，producer/consumer 用各自已配置的 trusted coordinator state root 驗證。

### R2 — Narrow private append

提供僅供 Manager producer seam 呼叫的 private append operation。輸入需含 expected full-registry raw-byte revision、exact run/work/repo/claim key、非空 `Sequence[PublicationReceipt]`（每筆均為 Child A strict typed value）、以及 producer 對同一 row 更新的一份明確欄位 patch。單筆 plan/PR append 是長度 1 的 batch；多輸出 brainstorm 必須在一個 batch 提交全部 receipts。操作不接受 caller-supplied dict，不做 publication side effect或證據推論。

在任何 mutation 前重新載入/確認 current snapshot、run row與 claim era。整個 batch 的 receipts 必須綁定同一 producer kind/event ID 與 repo/work/run/claim；batch 內 `(producer_event_id, publication_id)`、receipt ID 與 variant object key 均不得衝突。B 僅核 Child A typed values 的內部 digest/schema、current run/claim binding，以及所有 receipts 與同一份 coupled patch 的結構一致性；它不讀 workspace、GitHub 或 evidence sidecar，也不呼叫 C GET。#979/#980 必須在呼叫前驗實際 filesystem/remote side effect。若 PR variant，B 可檢查 receipt 內 create-witness tuple 與 published-object tuple相同；這只是結構一致，不是 B 認證 POST 或 GET。

### R3 — Atomicity and full-registry writer safety

硬依賴 #966 merged exact raw-byte revision CAS under canonical transaction lock; stale writer returns visible conflict and restores in-memory state. #862's slice-level binding CAS is narrower and cannot protect whole-file _persist(). Do not add merge/retry to stale append. No-lost-update test uses two processes against one registry snapshot and proves stale whole-file replacement is impossible.

All receipts in one non-empty append batch and the producer-coupled WorkflowRun fields are one registry snapshot commit and exactly one `_persist()` attempt. The filesystem/remote operation and immutable intent/result journal belong to #979/#980/#983; registry append only commits verified receipts and row fields. Any failed persist must retain the previous durable row and not expose an in-memory success.

#968 is a same-module field/API owner. Coordinate and serialize exact API/schema integration before production edits. #967 lifetime Manager lock does not substitute for #966, which covers all registry writers.

### R4 — Idempotency/collision contract

- A batch contains one producer kind/event ID and is either wholly new or an exact replay of the whole previously committed receipt set and identical coupled patch. Exact full-batch replay returns prior state without another row or coupled-field rewrite.
- A partly pre-existing batch, same pair/different payload, publication_id under another event, duplicate receipt_id with different payload, or same variant-specific object key under a different pair is conflict; no subset is appended. Planning key uses artifact kind/ref and PR key uses repository/number/id/node_id.
- One event may have multiple distinct publication IDs for different outputs; all are appended atomically as one batch with the single producer patch.
- Invalid container blocks append. An opaque malformed history row blocks only when uniqueness/replay cannot be established safely; it is never dropped or promoted.

### R5 — Reachable foreign planning artifact negative

Create a real temporary repo/workspace and authority whose confirmed source_revisions maps an existing foreign planning file. Submit formal work-action start and intake requests separately through the production request builder/daemon/Manager and production starter. The path internally discovers the file through work_bridge._artifact_rows; the request does not inject the private inner planning_artifacts parameter. On a fresh Registry reload, assert exact planning_authority repo/work/ref/kind/baseline hash and current file hash match, yet no producer event or valid receipt exists. Do not call _fallback_workflow_starter for the acceptance fixture.

### Acceptance criteria

- [ ] All ordinary Registry create/start/intake/update/reconstruction paths retain receipt state and reject request-facing injection/replacement/removal.
- [ ] Private append accepts a non-empty typed receipt batch; validates every item and exact run/claim/revision, appends all new receipts plus one coupled patch in exactly one `_persist()` using #966 full-registry CAS; exact full-batch replay is a no-op and partial/conflicting batches write nothing.
- [ ] Exact batch replay, same-event multiple outputs, partial prior batch, within-batch duplicate/collision keys, cross-run/claim, invalid container/row, stale revision, persist/rename/fsync/rollback failure all have zero false receipt and no lost prior row.
- [ ] Two-process race leaves either an explicit stale conflict or a serialized commit; never last-writer whole-file overwrite. Conflict is not auto-merged/retried.
- [ ] Formal start and intake negative fixtures use mapped pre-existing foreign files, production entrypoints/starter, fresh reload, exact authority/hash equality, and no valid receipt.
- [ ] Tests prove #862 slice CAS and #967 Manager lock cannot substitute for #966.
- [ ] No actual brainstorm/plan/PR producer mint or classifier behavior is claimed; no AC10 live canary is run here.

## Out of scope

No change to Child A schema/value, C GET, #979/#980 producer, #964 classifier or #965 downstream integrations. #847 remains full AC01–AC10 aggregate.
