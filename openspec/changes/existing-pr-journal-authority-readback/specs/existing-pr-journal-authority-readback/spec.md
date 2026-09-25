## ADDED Requirements

### Requirement: Existing Candidate PR journal authority reconciliation MUST preserve exact delivery identity

After #1069's same-run verify/review gate, the Manager MUST reconcile authority provenance on the existing delivery-journal row using the conditional-write API published by #983. #1069 only reads the journal row identity and durable revision; it does not mutate the row. #1070 owns the existing-row authority read-back and any permitted conditional update. This transition MUST preserve exact run_id/repository/work_id/claim-era/Candidate/PR identity, the existing transaction result, and the durable event history. It MUST NOT repeat #1069 reset eligibility, introduce a registry primitive, clone the #983 writer, or create a second journal store.

#### Scenario: Exact new-authority result already exists

- **WHEN** the exact run/Candidate/PR row, durable revision, transaction identity, canonical payload digest, and current authority digest/vector match
- **THEN** the Manager reads and returns the existing durable result
- **AND** it does not add a row, event, timestamp, transaction result, or remote PR side effect

#### Scenario: Existing row needs authority provenance synchronization

- **WHEN** #1054 source gates have validated the current authority digest/vector, the exact original PR is OPEN in the same repository with head equal to the Candidate, and the #983 API supports an exact-row conditional update at the current durable revision
- **THEN** the Manager updates only the authority provenance on that existing row and performs a fresh durable read-back
- **AND** the run, Candidate, PR, row identity, transaction result, timestamp, and other rows remain unchanged

#### Scenario: Conflict, unknown, or mismatched identity

- **WHEN** the journal revision is stale, an identity/payload conflicts, PR facts drift, or a conditional write/read-back remains unknown
- **THEN** the Manager returns a typed stop and retains the existing durable row
- **AND** an unknown result may be checked only by conditional read-back with the same transaction identity and canonical payload digest

#### Scenario: Process restart at a journal transaction boundary

- **WHEN** a fresh process re-enters before/after row load, conditional update, durable confirmation, or fresh read-back
- **THEN** it resolves the same transaction identity to the old row or the one confirmed update
- **AND** it creates no duplicate row, event, timestamp, result, or remote action

#### Scenario: Fixture uses the conflicted existing PR

- **WHEN** tests use workflow-52d048b72adbd5cae06f, Candidate 7ba7e877c94ff4eee72ba796ea9f8962953ed5cc, and stubbed PR #1049 as OPEN/DIRTY
- **THEN** the fixture remains fail-closed and never advances merge
- **AND** push, PR create/update, merge, and close spies all remain zero
