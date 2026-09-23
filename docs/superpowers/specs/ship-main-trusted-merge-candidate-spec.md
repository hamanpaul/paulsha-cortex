---
status: accepted
work_item: ship-main-trusted-merge-candidate
---

# 以受信任的 Builder merge candidate 完成 main 同步（#973）

## Scope and dependency

本票是 #943 Child B，blocked by #972。#972 的 live chain #987→#988→#989→#990 必須先合併，提供 push 前 probe、同 run 的 C/M/classification、recovery actions 與 durable stop API；它沒有數值 retry counter。#973 子票 B0 持有唯一 automatic budget；#990 仍是 needs_human stop writer。本票不新增 probe 或以浮動 main 取代 M。#943 完整驗收不縮減。

## Requirements

### R1 Bounded automatic repair and operator recovery

Manual retry-build and Manager automatic caller use the same same-run typed `(C,M,repair_kind,conflict_paths)` context. B0 persists a limit of one automatic dispatch reservation per exact tuple in existing `WorkflowRun.attempts`; `used` is durable and `remaining=max(limit-used,0)`. #972 does not own this counter. Candidate must CAS-match C. Automatic repair is permitted only for clean-behind or the unique CHANGELOG conflict while the B0 reservation can be made and reset is reachable. Exhaustion, invalid/missing tuple, C mismatch, unavailable M, disallowed conflict, or reset refusal/error stays a durable typed `needs_human` stop through #990, with existing recovery `next_actions` and no dispatch/preflight/push/PR/Copilot effect. Manual retry remains under #989 authority and does not spend the automatic budget.

### R2 Producer identity, selectors and exact-C provenance

Resolve direct Builder producers and Manager archive Candidate C. For archive lineage verify the unique successful archive job/evidence, real `C^1==B`, and unique successful Builder producer/evidence for B; B proves lineage only. Wire the shared resolver into `manager.resume_workflow_run` `jobs[-1]`, `_dispatch_workflow_card` reusable-job selection, and `work_bridge._builder_binding`. `_review_builder_job_binding` validates a review-supplied id and is not a retry selector. For a new post-archive Builder job, clone base and immutable `dispatch_head` are exact C; fix recording that currently copies historical B. `subject_head` is result metadata: absent before successful output and D when recorded after success; it is never the initial-base witness. Reuse only a unique current-era exact-C job whose task/evidence identity matches; otherwise fresh-dispatch exact C or typed stop.

### R3 Pinned true merge

Manual and automatic retry use one task builder bound to exact C, exact M, repair kind, all conflict paths, the Builder private ref containing M, and the B0 reservation identity for automatic work. Builder works in its isolated clone and never fetches or follows later main N. It creates a true non-fast-forward merge commit D with ordered parents `[C,M]`. Clean-behind and only the unique CHANGELOG `[Unreleased]` top-insertion conflict are eligible. For the permitted conflict, D preserves every complete entry from both sides exactly once, with Candidate entries first, and retains all other non-conflicting M tree changes. Every other conflict stops with complete paths.

### R4 Manager proof before source-ref promotion

Builder alone authors D and returns job output and a commit bundle. In the three-UID deployment Manager cannot inspect the Builder clone. It first imports the exact advertised bundle branch into a job-scoped quarantine ref using B4; this import must leave the source feature ref unchanged. Manager proves exact job/evidence/output binding, task C/M, real ordered parents `[C,M]`, classification, full tree and CHANGELOG invariants from the quarantined Git object. Only after proof succeeds may it call B4's atomic expected-old ref CAS to promote the source feature ref from C to D, then use the existing Candidate CAS from C to D. A concurrent `C→E` movement must fail the CAS even if E is an ancestor of D. Bad proof leaves source feature ref and Candidate at C and starts no D gate or delivery. Keep the quarantine proof ref for a safe exact retry after Candidate persistence interruption.

### R5 D requires fresh local gates

After D adoption every C-bound build, verify, test, review, maintainer-review, preflight and CI evidence is stale. Before any remote side effect, C1 reruns authoritative build harvest, verify/tests, cross-domain review, required maintainer review and local exact-head preflight on D. #847's same-content shortcut does not apply. Missing, failed, stale, wrong-head or ambiguous evidence blocks the ready-to-push handoff.

### R6 Push, PR and CI phases

Only after C1 local D gates pass may C2 push exact D. After push is confirmed at D, C2 creates or updates the authorized existing PR; an already mapped authorized PR may be reused. PR CI is read only after the PR is at exact D and must pass for that exact head before existing merge authority can proceed. Pending, failed, missing, stale, wrong-head or ambiguous PR CI blocks merge, issue closure and run completion. Preserve existing journal and closeout authority.

For a new PR create/adopt, #980/#982 provide the successful same-run POST witness and safe read-back; matching repository/base/head without that witness is ambiguous/needs_human. For an existing authorized PR mapped at C, C2 owns a separate durable C→D update intent/result in the delivery journal, bound to immutable PR identity and authenticated same-PR read-back. A lost update receipt may be reconstructed only from that intent plus remote D and same-PR read-back at D. In all cases preserve an external unwitnessed PR and do not rebind, merge or close it. Push/PR crash windows must reconcile exact D and the same authorized PR without pushing another SHA or creating a duplicate.

### R7 Pinned M and later recovery

If main moves from M to N, this repair still creates D from `[C,M]`; do not chase N. A new #972 probe owns a later C/M cycle. Existing manual/out-of-band recovery and not-mergeable resume remain available.

### R8 Parent acceptance and owner boundary

#943 parent acceptance remains intact, including probe timing, terminal/merged-PR exceptions, no delivery side effects on probe/merge errors, recovery `next_actions`, and PR/issue/run authority. Its local draft's old fail-open probe wording is a parent-owner handoff. #885 owns archive-applied/Aborted and active/archive coexistence behavior; those requirements are not implemented or duplicated here.

## Verification

Use bare-origin-backed real Git repositories and actual Builder clones. Cover clean-behind, allowed top-insertion CHANGELOG conflict, out-of-scope conflict, ordered `[C,M]` parents, complete entry retention/order, and non-conflicting M changes. Verify Builder clone initially lacks M while Manager source has exact M, private-ref transfer succeeds, missing source M fails closed, and main advancing M→N never substitutes N. Import bundle to quarantine and assert source feature ref remains C before proof; test bad binding/tree/evidence and a `C→E→D` race that leaves E intact. After proof, test exact Candidate adoption and replay after persistence interruption. Cover C-evidence invalidation, pre-push D gates, exact-D push, existing/new PR, lost push/PR receipts, external matching PR without same-run witness, post-push exact-D CI, and no merge/closure until required CI is green. No mocked merge substitutes for Git object assertions.

## Planning note

This is the complete acceptance umbrella, not an implementation slice. It spans six production modules (`manager.py`, `work_bridge.py`, `registry.py`, `work_actions.py`, `seams.py`, `job_workspace.py`) and cross-system state, so its actual fix-standard score is 8 / Red. Issue-backed Yellow descendants own implementation; #973/#943 AC remain whole and are not downscoped.
