---
status: draft
work_item: planning-baseline-rebind
issue: 1042
---

# Planning baseline rebind and verify recovery specification (#1042)

## Authority and scope

This draft is owned by [#1042](https://github.com/hamanpaul/paulsha-cortex/issues/1042). The live issue is the acceptance authority. #961 supplies the concrete pre-plan-review reproduction; #937 and #897 describe different drift classes and remain separate owners.

The change covers a planning source correction that is accepted before plan review, then becomes the plan-review baseline for the same run. It also defines an exact-run and exact-candidate recovery path for an already harvested candidate whose verify dispatch stopped on that stale baseline.

## Requirements

### R1042.1 — Bind the reviewed planning source before build dispatch

When Manager accepts the final plan review for a run, it MUST persist the exact planning artifacts that were reviewed: canonical ref, kind, work item, source revision, and SHA-256. The baseline update and the plan-review pass/build-phase transition MUST share one Manager-authorized Registry write boundary. A build card MUST NOT dispatch until that write succeeds.

The source revision MUST come from a Manager-validated canonical source or authority snapshot. Workspace bytes, caller-supplied hashes, an unmerged PR, or a changed file by itself MUST NOT establish accepted provenance. If the accepted source cannot be identified uniquely, Manager MUST stop without updating the baseline or dispatching build.

### R1042.2 — Preserve the prior fail-closed drift boundary

Only artifacts proven to be the exact source snapshot accepted by plan review may replace the run's earlier `planning_authority`. Each ref MUST remain bound to the same run work item and artifact kind. Missing, duplicate, malformed, symlinked, unaccepted, or source-ambiguous artifacts MUST remain rejected.

For `tasks.md` and `todo.md`, the existing checkbox-only normalization MAY account for candidate completion marks after the source ref and accepted SHA are independently proven. It MUST NOT forgive inserted tasks, rewritten descriptions, renumbering, or other content changes. Changes to spec or design bytes MUST remain exact.

### R1042.3 — Recover a pre-dispatch verify stop without changing the candidate

For an ongoing run stopped in verify because its frozen planning baseline predates a plan-review-accepted source, a formal recovery MUST be bound to both an exact `expected_run_id` and exact `expected_candidate`. Manager MUST recompute the eligible accepted source and candidate planning hashes from trusted run/source data; request arguments cannot supply or override that evidence.

Recovery is eligible only when the run is still the unique active run for its canonical WorkAuthority, is in verify with the planning-input-drift stop, has the exact candidate, has no active job, and has not already dispatched verification for that candidate. Manager MUST confirm that candidate planning inputs equal the accepted source after only the existing checkbox normalization. The recovery MUST preserve the candidate SHA and route that candidate through the normal verify gate; it MUST NOT mark verify passed or reuse another run's test/gate evidence.

### R1042.4 — Use a fresh run when exact recovery is unavailable

If the original run is abandoned, superseded, lacks a unique accepted source, has a missing or changed candidate, has already dispatched verify, or fails any recovery predicate, candidate-preserving recovery MUST be rejected. The supported fallback is a fresh `work start` from current confirmed WorkAuthority and accepted planning artifacts. A new run MUST not inherit the old candidate, verification state, gate ledger, or completion claim.

### R1042.5 — Make re-entry auditable and idempotent

Every accepted baseline update or candidate-preserving recovery MUST record the run, old and new planning hashes, source revision, plan-review evidence, candidate SHA when present, actor/reason for operator recovery, and before/after run state. Evidence MUST be Manager-owned and immutable; no operator may edit registry or evidence files directly.

Repeating the same accepted transition MUST return the existing result without a second baseline write or verify job. A stale run/candidate/source CAS, changed source, duplicate source, or conflicting re-entry MUST fail closed and dispatch no job.

## Scenarios

### Scenario: accepted pre-plan-review correction is frozen

- **WHEN** a canonical planning correction is merged and is the exact source reviewed by the final plan-review gate
- **THEN** Manager records that source's hashes and provenance in the same transition that passes plan review, before any builder dispatch

### Scenario: #961 todo correction and candidate checkboxes

- **WHEN** the accepted source todo hash is `cd55c7ce9c3e202d9057f764c88a42b8fc294de4618e85e90433e5e1b3ce544e` and a candidate changes only checkbox markers, producing `cb390f92be32801dd6c4d276a2d99ea96205ead5ece181995249fe582030016f`
- **THEN** source verification may accept the candidate planning input after the existing checkbox normalization, while preserving its exact Git candidate `8defa4ca9c8f2a310b1dc37d36d69de1580ce4d4`

### Scenario: candidate recovery is exact and pre-dispatch only

- **WHEN** an operator requests recovery with the correct run ID and candidate SHA and all R1042.3 predicates hold
- **THEN** Manager records the rebind receipt and dispatches normal verification for that same candidate exactly once

### Scenario: unknown or expanded correction is rejected

- **WHEN** the source merge/provenance is unknown or unmerged, the candidate changes any spec/design content, the todo has non-checkbox edits, or run/candidate CAS does not match
- **THEN** Manager preserves the fail-closed stop and creates no verify job

### Scenario: old run is no longer eligible

- **WHEN** an exact candidate remains in Git but its original run is abandoned or superseded
- **THEN** the candidate remains historical evidence only; a new run starts from current confirmed authority and owns fresh verification evidence

## Non-goals

- Do not widen the builder's planning-edit tolerance for the incremental task restructuring in #937.
- Do not implement #897 harvest-time planning rejection, ancestor-candidate adoption, provider quota fallback, or retirement behavior.
- Do not change #961's claim authority reconciliation or its issue acceptance.
- Do not make `recover-planning` applicable outside its existing define-phase contract.
- Do not modify registry/evidence state manually or infer completion from a green builder gate alone.
