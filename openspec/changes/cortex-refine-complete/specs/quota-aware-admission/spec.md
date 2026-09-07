## ADDED Requirements

### Requirement: Sourced multi-pool observations and forecasts

Cortex SHALL retain pool/account scope, units, windows, observation time, reset, TTL and observed/estimated/unknown provenance. Forecasts SHALL include uncertainty, context, task/profile applicability and failed-attempt consumption without treating model tokens as subscription quota.

#### Scenario: Missing balance
- **WHEN** a provider exposes no authoritative remaining quota
- **THEN** admission reports unknown or partial coverage and follows explicit bounded-risk policy rather than claiming confirmed sufficient balance

#### Scenario: Multiple windows
- **WHEN** short-term allowance is sufficient but a shared weekly pool is exhausted
- **THEN** the candidate is not admitted and renaming the model within that same pool does not bypass the restriction

### Requirement: Atomic reservation and reconciliation

Cortex SHALL reserve all required managed pools atomically before spawn and reconcile actual consumption, uncertain liveness and provider observations durably across restart.

#### Scenario: Competing instances
- **WHEN** two managed instances contend for one remaining task-sized allowance
- **THEN** only one obtains the reservation and the other re-evaluates without spawning against stale balance

#### Scenario: Restart with uncertain job
- **WHEN** the budget authority restarts and cannot confirm whether a reserved job remains alive
- **THEN** it preserves uncertain consumption/reservation and does not release allowance solely because a TTL elapsed

#### Scenario: Corrupt cooldown state
- **WHEN** persisted backoff or quota state is corrupt
- **THEN** the system reports degraded/unknown or uses validated last-good state, never treating corruption as confirmed full allowance

### Requirement: Qualified dynamic fallback

Cortex SHALL select among compatible qualified profiles using current task demand, available resources and versioned preferences, preserving explicit pin, credentials, tools and reviewer independence. Rerouting SHALL occur at safe attempt boundaries with supersession evidence.

#### Scenario: Independent usable pool
- **WHEN** profile A is qualified but lacks quota and qualified profile B has sufficient independent allowance
- **THEN** Cortex reserves and dispatches B without first launching a predictably failing A job

#### Scenario: No compliant fallback
- **WHEN** only candidates violating an explicit pin or reviewer independence have quota
- **THEN** Cortex waits with a specific reason instead of silently relaxing the constraint

#### Scenario: Forecast misses external consumption
- **WHEN** unobserved external activity causes in-flight quota failure
- **THEN** Cortex preserves verified artifacts, records the affected pool and safely supersedes the attempt before rerouting, retaining the forecast error as calibration evidence
