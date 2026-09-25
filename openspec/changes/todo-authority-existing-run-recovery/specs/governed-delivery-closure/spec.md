---
status: draft
work_item: todo-authority-existing-run-recovery
issue: 1055
---

## ADDED Requirements

### Requirement: Existing-Candidate recovery MUST bind fresh Todo authority to an exact run and Candidate

Manager MUST enter this recovery only after #966 raw-revision JobRegistry CAS and #1054 plus #1063, #1064 and #1065 have actually merged and their accepted contracts are freshly verified. It MUST require one valid canonical Todo source and compare the full old/new WorkAuthority source revision vector/digest to the exact persisted run/Candidate/claim/source/gate/job/PR tuple with a durable registry CAS. A Draft upstream PR, read-model registry sequence, caller-supplied digest, or path-link override MUST NOT stand in for current source authority.

#### Scenario: Todo is linked but not yet scanner-confirmed

- **WHEN** a path link exists but the fresh provider snapshot does not confirm exactly one owner-published canonical Todo with matching issue/work provenance
- **THEN** the Manager leaves the run, registry, journal, jobs and PR unchanged and returns a typed source-not-ready reason

#### Scenario: Source digest changes after Todo publication

- **WHEN** the source revision vector/digest changes between initial load and the exact recovery transition
- **THEN** the registry CAS rejects without changing the run or dispatching verify/review work
- **AND** the next attempt requires a fresh explicit operator resume and complete revalidation

### Requirement: Existing-Candidate recovery MUST preserve old evidence and prevent duplicate delivery

For the same exact ongoing run with one existing Candidate and one matching open PR, successful recovery MUST atomically invalidate only verify/review evidence from the old claim era, retain the Candidate/build history/old job identities/PR refs, and run verify/review again on that exact Candidate under the new claim era. Delivery MUST use the landed #983 conditional journal write and exact same-PR read-back contract; same identity plus same payload is an idempotent no-op, while mismatch/unknown MUST fail closed. The recovery path MUST NOT push, create another PR, merge, close, rewrite historic job/evidence bindings, abandon or create a replacement run.

#### Scenario: Existing PR exactly matches Candidate

- **WHEN** the exact old run/Candidate tuple passes CAS, the one open PR head matches Candidate, all source and journal facts are fresh, and the operator explicitly resumes
- **THEN** only old verify/review evidence is invalidated, the same Candidate is re-verified once, and read-back returns the existing PR with zero push/create/merge calls

#### Scenario: PR is conflicting, stale or unknown

- **WHEN** the existing PR is open but conflicting with main, its head differs from Candidate, it is merged/closed, duplicated, or remote facts are degraded
- **THEN** the Manager does not alter the run, Candidate, journal or PR and returns a typed fail-closed reason

#### Scenario: Recovery is repeated after a crash

- **WHEN** the same explicit request is repeated after registry CAS, job creation, journal write or read-back may have committed
- **THEN** exact identity read-back returns the prior durable result or a typed conflict/unknown result
- **AND** no duplicate gate job, journal event, push, PR, merge or false closure is created

#### Scenario: Recovery must not abandon an existing Candidate

- **WHEN** current source, registry, PR or journal facts do not satisfy exact recovery CAS
- **THEN** the Candidate, PR, history and journal remain queryable under the original run
- **AND** no abandon, supersede, retirement or replacement run is used as a substitute
