## ADDED Requirements

### Requirement: Extensible execution profiles

Cortex SHALL separate task role/demand from executor, model and adapter-native effort; supported profiles SHALL be resolved through versioned capability descriptors rather than a central product-name table.

#### Scenario: Add a new model and effort
- **WHEN** an existing adapter receives a new model and previously unseen supported effort through its descriptor
- **THEN** it participates in validation and selection without a product-specific central resolver change, while unsupported combinations fail before spawn

#### Scenario: Add a new runtime
- **WHEN** a new executor adapter passes capability and Trust Root conformance
- **THEN** workflow, quota and report consumers use the shared contract without adding that runtime's product-name branch

### Requirement: Profile-aware qualification and provenance

Cortex SHALL validate imported PatchMUD evidence against the requested role, measured dimensions, compatible execution profile and producer schema; it SHALL preserve explicit review approval and report requested/resolved/observed values separately.

#### Scenario: Effort mismatch
- **WHEN** stored evidence measures high but the candidate requires max
- **THEN** Cortex does not claim that evidence measures the requested profile and records legacy/unknown or requires matching qualification

#### Scenario: Pricing-only revision
- **WHEN** only a pricing snapshot changes
- **THEN** cost-report provenance changes without invalidating the capability fingerprint solely due to that price revision

#### Scenario: Unmeasured role
- **WHEN** a report contains only builder coverage
- **THEN** planner/reviewer and unmeasured dimensions remain unknown and report success does not itself grant runtime authorization

### Requirement: Evidence plane isolation

Cortex SHALL consume versioned CLI/file reports without importing PatchMUD runtime; qualification, operational telemetry and live quota SHALL remain distinct evidence classes.

#### Scenario: Producer unavailable
- **WHEN** PatchMUD is absent or its required producer schema is not yet delivered
- **THEN** current runtime reports the qualification limitation without running benchmark work in the dispatch hot path or inventing an approved roster
