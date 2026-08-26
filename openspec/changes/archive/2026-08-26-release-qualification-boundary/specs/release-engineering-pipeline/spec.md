## ADDED Requirements

### Requirement: GitHub Release MUST require deterministic exact-SHA qualification

Before creating a GitHub Release, the release workflow MUST locate a successful manual release
qualification run for the exact default-branch candidate SHA, download and validate schema-v2 evidence,
and prove that its candidate SHA, wheel hash and bundle hash match the release build. The release
qualification MUST NOT require live credentials, provider availability, external repository mutation or a
deployment canary result. Missing, stale, skipped, profile-mismatched or inconsistent evidence MUST fail
closed.

#### Scenario: deterministic qualification unlocks the same SHA

- **WHEN** release-profile qualification for candidate A passes all installer, systemd, attestation and attack-matrix gates
- **THEN** the evidence remains bound to A without changing any external repository ref
- **THEN** release may proceed only while default-branch HEAD and the rebuilt wheel/bundle still match A's evidence

#### Scenario: qualification depends on live secrets or external writes

- **WHEN** the release qualification workflow requests a provider/GitHub secret, calls a live provider, or executes an external repository mutation
- **THEN** the workflow contract is invalid and MUST NOT unlock a GitHub Release

### Requirement: Live deployment canary MUST be isolated from the release gate

Provider authentication/quota/model checks, Manager GitHub probing and full intake-to-closeout validation MUST
run only as a separately named, manual deployment canary using a protected environment. Canary
evidence MUST use the `deployment-canary` profile and a distinct artifact name. The release workflow MUST
NOT query, download or require deployment-canary evidence.

#### Scenario: provider or probe repository is unavailable

- **WHEN** a deployment canary cannot authenticate, has insufficient quota, observes model fallback, changes unexpected refs or cannot reach terminal closeout
- **THEN** the canary fails closed and reports the deployment-health failure
- **THEN** that failure alone does not invalidate an otherwise matching deterministic package qualification

#### Scenario: canary evidence is presented to release

- **WHEN** release validation receives schema-valid evidence whose profile is `deployment-canary`
- **THEN** validation fails before tag or GitHub Release creation
