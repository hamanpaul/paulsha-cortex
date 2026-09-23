---
status: accepted
work_item: producer-lineage-resolver
---

# Builder producer lineage resolver 規格（#973 Child A）

## Requirements

本票是 #973 的 issue-backed 子工作；#973 是 #943 Child B，並 blocked by #972。此票只提供 Manager 可共用的唯讀 producer lineage resolver；任何 automatic repair 仍等 #972 merged 與自動重派子票接線。

### R1 一般 Builder producer
解析 exact Candidate 的唯一 Builder job 時，驗證 run/job 身分、job terminal success、既有 build evidence、subject/output SHA 與 current claim era。缺漏、重複、失配或不確定時不得猜選。

### R2 Manager archive Candidate lineage
當 Candidate C 是 Manager archive commit 時，驗證同一 run 的唯一成功 openspec-archive job 與其既有成功 evidence；從實際 Git commit objects 驗證 C 的第一 parent 是 B，並解析唯一成功 Builder job 產生 exact B。B job 只證明 archive lineage，不能當成 C 或後續 D 的現行 Builder producer。

### R3 Read-only / fail-closed
Resolver 僅讀 WorkflowRun、job records、綁定 evidence 及 Git objects。不得寫 registry、WorkflowRun、job、claim era 或 refs；任何歧義以 typed failure 回報給 selector，由 consumer fresh-dispatch exact Candidate 或停 needs_human。

### R4 Owner boundary
Archive command 是否實際 apply、Aborted/no-op 語意、active/archive coexistence guard 屬 #885，不在本票重複實作。此票只判定 #973 明列的既有成功 job/evidence 與 B→C Git lineage。

### R5 Actual retry selectors
Wire the resolver into the actual consumers: manager.resume_workflow_run's jobs[-1] selection, _dispatch_workflow_card's reusable job selection, and work_bridge._builder_binding. _review_builder_job_binding only validates a review-supplied id and does not fix retry selection. The two retry selection sites must exclude the historical B job and may reuse only the valid current-era exact-C job.

### R6 Post-archive dispatch provenance
For a new post-archive Builder dispatch, clone base and immutable dispatch_head must be exact C. Fix the recorder path that currently derives dispatch_head from the historical Builder job and may persist B. subject_head is a build result field: it may be absent before successful output and becomes D after success. Prove C/M in the B1 task input and output D in result evidence; do not treat subject_head as the initial base witness.

## Verification

用真 Git fixture 建立 B 與兩-parent archive commit C；覆蓋 direct Builder candidate、成功／失敗／缺漏／重複 archive job、錯誤 C parent、缺漏／重複／錯誤 B producer、失配 evidence、舊 claim era。證明 resolver 不改任何 registry value 或 ref。拒絕條件不得回傳替代 producer。

## Boundary

Production scope is manager.py resolver/selection/recording and work_bridge.py ship selector wiring. Automatic retry, D proof/adoption and gates remain separate #973 descendants. Maintain #765 current-era Builder authority.
