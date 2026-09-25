## ADDED Requirements

### Requirement: Manager MUST persist typed main-sync stop context on the same WorkflowRun

The Manager MUST persist the needs_human facet, `delivery-needs-human` reason, and the updated #988 typed `context.main_sync` in one durable update to the same WorkflowRun. It MUST preserve the original delivery reason/detail and all sibling context values. The nested JSON value MUST preserve validated C, or the original invalid/abbreviated pre-validation Candidate observation as diagnostic-only according to the updated #988 wire contract; it MUST preserve M or null, every complete conflict path, repair kind, skipped reason, and typed failure members without stringification or truncation. A pre-validation Candidate failure MUST have M=null and MUST NOT authorize retry-build.

#### Scenario: Passed-review advance-ship replay stops for main-sync

- **WHEN** `apply_workflow_action` replays `advance-ship` after the review card passed and the ship validator returns a manual main-sync stop
- **THEN** the Manager wrapper persists needs_human state and typed `context.main_sync` for the same run
- **THEN** a read from registry storage after the wrapper returns reproduces every nested field and the original delivery reason/detail exactly

#### Scenario: Final review evidence advance-phase stops for main-sync

- **WHEN** the final review evidence completes and the `advance-phase` transition reaches the same manual main-sync stop
- **THEN** the Manager wrapper uses the same durable writer
- **THEN** a read from registry storage after the wrapper returns reproduces every nested field and the original delivery reason/detail exactly

#### Scenario: Candidate validation failure preserves the observation without authority

- **WHEN** the probe stops before Candidate validation because C is invalid or abbreviated
- **THEN** the Manager persists the original observed C value through the updated #988 typed diagnostic contract and keeps M and `failure.main_head` null
- **THEN** registry read-back preserves the failure and the status/action projection does not expose retry-build

#### Scenario: Failure after resolving M retains that M

- **WHEN** the probe resolves a valid M and a later failure is wrapped as needs_human
- **THEN** `context.main_sync.main_head` and `context.main_sync.failure.main_head` remain that exact M after store read-back
- **WHEN** no valid M was resolved
- **THEN** both values remain explicit null after store read-back

### Requirement: Status hints MUST follow recovery action availability

The status projection MUST use the #989 recovery helper's action availability/result. It MUST NOT advertise retry-build when that helper says the action is unavailable, when C is missing/invalid/abbreviated, or when M is null. Missing/corrupt context or failed/mismatched durable persistence MUST fail closed and suppress retry-build availability.

#### Scenario: Main-sync stop action hints

- **WHEN** a main-sync stop has durable context and the recovery helper returns available retry-build or resume actions
- **THEN** `workflow_status_entry` shows the matching next-step hint
- **WHEN** the helper does not expose retry-build
- **THEN** status does not describe retry-build as available

### Requirement: Future skipped-repair stops MUST use the same writer without running repair

The shared writer MUST accept typed synthetic contexts for `repair-budget-exhausted` and `registry-reset-refused` and preserve their C or diagnostic-only invalid-C observation, M/null, full paths, repair kind, skipped reason, and failure through same-run store read-back. This requirement does not authorize automatic repair, reset, Builder dispatch, merge, or push.

#### Scenario: Repair budget exhausted synthetic stop

- **WHEN** a typed context records `skipped_reason=repair-budget-exhausted`
- **THEN** the writer stores it on the needs_human WorkflowRun and read-back returns it exactly
- **THEN** no repair/reset/Builder operation runs

#### Scenario: Registry reset refused synthetic stop

- **WHEN** a typed context records `skipped_reason=registry-reset-refused`
- **THEN** the writer stores it on the needs_human WorkflowRun and read-back returns it exactly
- **THEN** no repair/reset/Builder operation runs
