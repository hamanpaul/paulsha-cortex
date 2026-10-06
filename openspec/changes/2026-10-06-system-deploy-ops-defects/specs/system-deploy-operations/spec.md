---
status: accepted
work_item: system-deploy-ops-defects
---

## ADDED Requirements

### Requirement: System Monitor reads a Manager-synchronized source checkout

The system Monitor MUST access the Manager-owned source checkout read-only. The Manager MUST fetch the advertised GitHub default branch without writing `FETCH_HEAD`, and MUST fast-forward only when the tracked checkout is clean and the update is a fast-forward. Each Manager git command MUST have a finite timeout. Unsafe or unavailable updates MUST leave the checkout unchanged, and a timeout MUST NOT prevent Manager from running its periodic tick.

#### Scenario: Read-only Monitor sees newly synchronized work

- **WHEN** GitHub's default branch advances with a new work item and the Manager checkout is clean and behind
- **THEN** Manager advances the checkout and the next Monitor scan can read the new work item without Monitor writing the checkout

#### Scenario: Dirty or diverged checkout

- **WHEN** the Manager checkout has tracked local changes or cannot fast-forward to the advertised default branch
- **THEN** synchronization leaves it unchanged and reports the condition

#### Scenario: Source sync command times out

- **WHEN** a Manager git command exceeds its timeout while synchronizing the source checkout
- **THEN** synchronization reports the timeout, leaves the checkout unchanged, and Manager continues with the periodic tick

### Requirement: Provider freshness and refresh intervals share one bound

Monitor configuration MUST keep the refresh interval below the effective provider stale threshold, and the stale threshold MUST NOT exceed the claim provider maximum age of 900 seconds. System and user-level configurations MUST use the same validation.

#### Scenario: Invalid refresh policy

- **WHEN** the refresh interval is greater than or equal to the stale threshold, or the stale threshold exceeds 900 seconds
- **THEN** Monitor configuration loading rejects the policy

### Requirement: System deployment supplies safe default admission and workspace settings

When no operator quota-pools file exists, the system Manager MUST produce quota shadow admission context bound to packaged identities without enforcing quota. The system Monitor MUST resolve its project workspace from `PSC_REPO_ROOT` rather than operator home.

#### Scenario: No operator quota file

- **WHEN** system Manager starts without an operator quota-pools file
- **THEN** it records shadow admission decisions for installed identities and does not enforce capacity limits

#### Scenario: Isolated system Monitor

- **WHEN** the system Monitor loads project configuration
- **THEN** its workspace resolves to the instance repository root even if a retained project config names an operator-home path

### Requirement: Supported model identities are available to system deployment

The packaged model roster MUST include supported builder identities `codex/gpt-6-luna` and `copilot/gpt-5.4-mini`, and the planning/review identity `agy/gemini-3.8-flash-high` MUST NOT have build capability. System deployment MUST provide a validated command to update its Manager-owned overlay without hand-editing the protected file.

#### Scenario: Packaged and system overlay identities

- **WHEN** the packaged roster is loaded or an operator adds an identity through `cortex model identity add`
- **THEN** identities are validated, the packaged identities are recognized, and the Manager-owned overlay is atomically updated while preserving owner and mode

### Requirement: Adoption parks existing automatic slice specs once

Manager MUST reversibly park the existing top-level `dispatch: auto` slice specs once during adoption/upgrade. A durable marker MUST keep later specs eligible, and parked contents MUST remain recoverable.

#### Scenario: First Manager start after adoption

- **WHEN** existing automatic slice specs are present and no migration marker exists
- **THEN** Manager moves them intact into a dated hidden directory and records the migration

#### Scenario: Later automatic spec

- **WHEN** Manager starts after the migration marker exists and a new automatic slice spec is present
- **THEN** Manager leaves the new spec in the active specs root
