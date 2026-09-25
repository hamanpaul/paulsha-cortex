---
status: draft
work_item: same-run-authority-restart
issue: 1069
---

# Same-run authority restart for an existing Candidate Specification

## Scope

This change is owned only by issue [#1069](https://github.com/hamanpaul/paulsha-cortex/issues/1069), `work_item: same-run-authority-restart`. It consumes #1054/#1063/#1064/#1065 and #1068 after their hard prerequisites land. #1068 itself depends on #966. #1070 owns complete existing-PR journal authority reconciliation and consumes #983's conditional writer.

Production scope is one existing module, `paulsha_cortex/coordinator/work_actions.py`. This spec does not authorize intake or implementation while dependencies are open.

## Requirements

R1–R9 below are the complete requirements for this issue-bound change; each requirement includes a deterministic scenario.

## ADDED Requirements

### Requirement: R1 Explicit operator-only recovery

Manager MUST enter same-run authority restart only after an explicit operator `work resume` for the exact repo/work item. Periodic scan, automatic claim, start, and intake MUST NOT reset claim/source era or dispatch recovery.

#### Scenario: Automatic scan sees new Todo authority

- **WHEN** a periodic/automatic scan observes changed or newly qualified Todo authority for an existing Candidate run
- **THEN** it leaves the run and gates unchanged and dispatches no recovery job.

#### Scenario: Explicit operator resume

- **WHEN** the operator explicitly resumes the exact repo/work item and one eligible run is found
- **THEN** Manager continues only that exact same run after all preconditions pass.

### Requirement: R2 Fresh qualified WorkAuthority

Manager MUST consume the published #1054 admission and #1063/#1064/#1065 authority contracts. The authority MUST identify one owner-published canonical Todo with matching issue/work-item provenance, matching `work_item` metadata, and concrete Tasks, observed by the latest successful fresh Monitor generation. Manager MUST preserve old/new sorted source revisions, snapshot hash, provider revision, and full authority digest. Overrides, fuzzy path matches, and registry sequence MUST NOT substitute for source authority.

#### Scenario: Stale or ambiguous Todo authority

- **WHEN** the Todo is missing, ambiguous, unqualified, stale, or not visible in a successful current generation
- **THEN** recovery stops typed before reset or dispatch.

#### Scenario: Source changes after publication

- **WHEN** the new Todo/source digest changes between authority capture and restart
- **THEN** recovery stops typed and preserves the existing Candidate/PR/history.

### Requirement: R3 Exact same-run Candidate eligibility

The expected run, repo, work ID, status, verify/review phase, retry classification, old claim key, old source revision, Candidate SHA, verified head, gate/evidence refs, job refs, and PR refs MUST match one exact snapshot. The Candidate MUST exist, the run MUST be an eligible ongoing verify/review state, and no active job may exist. Superseded/other runs, another repo/work item, missing Candidate, active job, wrong claim era, and terminal PRs MUST fail closed.

#### Scenario: Tuple drift

- **WHEN** any run/claim/Candidate/gate/job/PR field differs from the captured exact tuple
- **THEN** no reset occurs and no job is dispatched.

### Requirement: R4 Read-only journal and authenticated PR preflight

Before reset, Manager MUST read back the existing delivery-journal row's exact identity/revision without modifying it, and MUST use authenticated GitHub reads to establish one original open PR on the same repository whose head equals the Candidate SHA and whose state is unchanged. Missing, unknown, conflicting, or mismatched facts MUST fail closed. This change MUST NOT write the journal or perform push/create/update/merge/close PR or other delivery mutations; #1070 owns complete journal authority reconciliation.

#### Scenario: Journal or PR fact unknown

- **WHEN** exact journal identity/revision or authenticated PR identity/head/state cannot be read unambiguously
- **THEN** recovery stops before CAS and preserves existing artifacts.

### Requirement: R5 Single exact WorkflowRun CAS

After preflight, Manager MUST invoke the #1068 domain-specific exact transition once, supplying the durable registry revision, full expected run tuple, and new digest/source binding from verified WorkAuthority. A stale revision, tuple conflict, changed payload under the same request identity, persistence unknown, or concurrent resume MUST produce typed fail-closed output. This change MUST NOT add a registry primitive or write registry files directly.

#### Scenario: Concurrent or stale resume

- **WHEN** the registry revision or expected tuple changes before commit
- **THEN** the transition reports conflict and the old durable/memory state remains authoritative.

### Requirement: R6 Post-CAS revalidation before verify/review dispatch

After CAS success, Manager MUST freshly reload WorkAuthority, same run/new claim era, same Candidate, same open PR/head/state, and exact no-active-job state. Any digest/source/provider/PR/job drift MUST stop dispatch. On success, Manager MUST dispatch only verify/review for the original Candidate; build MUST NOT rerun and no replacement run may be created.

#### Scenario: Post-CAS drift

- **WHEN** authority, registry, Candidate, PR, or active-job facts change after CAS
- **THEN** the gates remain stopped and no new job is dispatched.

### Requirement: R7 Preserve old evidence and Candidate history

Restart MUST preserve run identity, Candidate, build/repair results, old JobRegistry rows, old claim bindings, old evidence refs/bytes, PR refs, journal row, and history. Only verify/review gates are invalidated. New verify/review jobs/evidence MUST bind the same run/repo/work/Candidate and new claim era; old evidence MUST NOT satisfy new gates.

#### Scenario: New authority era uses old verify evidence

- **WHEN** old verify/review evidence is presented after authority restart
- **THEN** it remains historical and cannot satisfy the new era's gate.

### Requirement: R8 Fixture-only recovery evidence

Real Manager/WorkAuthority fixtures and stub GitHub reads MUST cover the accepted path and all negative/crash/replay/concurrency conditions using the issue's #983 run/Candidate/PR tuple. Every remote/write spy MUST remain zero. The fixture MUST NOT operate on the live #983 run or PR #1049, and fixture results MUST remain distinguished from live provider evidence.

#### Scenario: Fixture positive and negative matrix

- **WHEN** unique fresh authority is supplied, then each missing/stale/drift/wrong tuple/active job/CAS/crash/replay/concurrency negative is exercised
- **THEN** only the positive same-run verify/review fixture may advance, with all delivery mutation spies at zero.

### Requirement: R9 Narrow integration boundary

The production diff MUST remain `work_actions.py` only. The feature MUST NOT add a source resolver, path scanner, Todo parser, registry primitive/writer, journal writer, CLI command, or delivery mutation. It MUST NOT use abandon, recover-pre-candidate, supersede/retire the original run, or create a replacement run. If a required accepted API is absent or another production module/cross-writer API is required, implementation MUST stop for issue-backed re-scope.

#### Scenario: A second production module or writer is required

- **WHEN** an accepted dependency is missing a required API or implementation needs another production module, registry writer, journal writer, or delivery mutation
- **THEN** implementation stops for issue-backed re-scope and does not widen this change.
