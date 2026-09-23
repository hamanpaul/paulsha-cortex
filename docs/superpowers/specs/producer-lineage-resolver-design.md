---
status: accepted
work_item: producer-lineage-resolver
---

# Producer lineage resolver 設計（#973 Child A）

## Decisions

### D1 Resolve lineage as evidence, not mutable selection
單一 Manager helper 接收 run、Candidate SHA 與 registry/Git readers，回傳 exact producer binding 或明確的 unresolved reason。它不更改候選、job order、claim era 或 refs。

### D2 Verify archive commit ancestry with Git
對 archive Candidate C，先確認唯一 successful Manager archive job/evidence，再從 Manager 可讀的 commit object 驗證 C 的第一 parent B。之後以既有 build evidence 綁定唯一 Builder producer of B。job summary 中的 SHA 只是候選線索；Git parent 關係與 evidence identity 才是證據。

### D3 Historical B is not current producer
成功的 B job 只證明 C 的 lineage。它不得被回傳為 current Builder producer，也不得允許任何 caller 對 B job 做 retry reuse。後續 current-era job 必須另外從 exact C 派出並由 selector integration 正確記錄。

### D4 Keep #885 separate
此 resolver 不執行 archive、不判定 active/archive trees 是否共存，也不重新定義 archive Aborted/no-op。它只消費 archive job 的既有成功 evidence，避免重做 #885 的責任。

### D5 Failure returns no guessed binding
缺 job、重複 job、evidence mismatch、parent mismatch、invalid SHA 或 Git object 不可用都回傳不可採信結果。由呼叫端決定 exact-C fresh dispatch 或 needs_human；不可在 resolver 內 fallback 到舊 B。

### D6 Wire the real selectors and record the chosen base
Use this resolver from manager.resume_workflow_run's jobs[-1] selector, _dispatch_workflow_card's reusable-job selector, and work_bridge._builder_binding. Do not claim _review_builder_job_binding is a retry selector; it validates an id supplied by review. The Manager dispatch record must set immutable dispatch_head=C for post-archive retry, never infer it from historical B. subject_head may be absent at dispatch and records successful output D; exact C/M are bound by the B1 task input and D remains in result/evidence.

### D7 Preserve reuse and crash-window rules
Reuse only a unique successful current-era job whose recorded base is C and whose output/evidence binds to the same run and task. Old-era, B-based, malformed or ambiguous jobs are excluded; when no eligible job exists, fresh dispatch from C or typed stop.

## Verification matrix

| Case | Required result |
|---|---|
| Direct current Builder | 唯一 exact Candidate job/evidence 得到 binding |
| Valid archive C | Manager job/evidence + real C^1=B + unique B Builder evidence 得到 lineage |
| Bad/missing/duplicate archive | unresolved；不回傳 B |
| Bad/missing/duplicate B job | unresolved；不回傳 archive producer |
| Wrong era or populated successful result subject mismatch | current producer 不可採信; subject_head may be absent before output |
| Mutation trap | Resolver reads leave WorkflowRun, job rows, claim key and refs byte-identical |
| Retry selectors | resume jobs[-1] and dispatch reusable selection choose only eligible C-based current-era job |
| Dispatch recording | New post-archive job stores dispatch_head=C; subject_head transitions from absent to output D |

## Sizing boundary

本票 touches manager.py and work_bridge.py, so domain_breadth=1. It reads multi-record lineage but only corrects the existing new-job provenance fields; it adds no new writer, schema or cross-object transition, so state_consistency=1. With complete accepted planning and fix-standard mechanics its score is 6 / Yellow.
