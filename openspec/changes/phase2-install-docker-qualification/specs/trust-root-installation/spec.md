# trust-root-installation Specification

## Purpose

以 hash-bound desired state、root-owned receipt 與 fail-closed verification，提供四分 UID
trust-root 的可重建、可 replay、可回滾安裝流程。

## ADDED Requirements

### Requirement: plan MUST be unprivileged, canonical, four-way, and secret-free

`cortex install trust-root plan` MUST NOT require root. It MUST emit canonical structured desired
state bound to exact candidate wheel/bundle hashes, repo identity, the `four-way` scheme, principals,
assets, ownership/modes/ACLs, generated surfaces, toolchain manifest, apply order and legacy policy.
It MUST reject secret-like fields, symlinks, path escapes and unsupported schemes.

#### Scenario: deterministic plan hash

- **WHEN** the same safe config and bundle are planned twice
- **THEN** both canonical documents and their SHA-256 digests are identical
- **THEN** neither document contains credential bytes or operator HOME discovery

### Requirement: apply MUST be receipt-backed, replayable, and fail closed

Privileged apply MUST require the exact plan SHA-256, complete OS/account/path/service/in-flight
preflight, consume typed registry/permgen data, atomically install the exact candidate from a locked
wheelhouse, and journal every mutation in a root-owned receipt. Existing state MUST only be adopted
when its identity and metadata match; unknown drift MUST NOT be overwritten.

#### Scenario: interrupted apply resumes safely

- **WHEN** apply is interrupted after one journalled step and rerun with the same plan/hash
- **THEN** the completed matching step is adopted without mutation
- **THEN** remaining steps continue exactly once
- **THEN** a mismatching completed asset stops replay before overwrite

### Requirement: credentials MUST use explicit bounded imports without disclosure

Credential import MUST require receipt, principal, provider and source. Provider adapters MUST accept
only allowlisted regular files, reject symlinks/special files, atomically write normalized destinations,
and expose only provider/principal/mode/hash metadata in output, logs and receipts.

#### Scenario: source content is never rendered

- **WHEN** an allowed credential file is imported
- **THEN** output and receipt contain its SHA-256 but not its bytes or secret-like values
- **THEN** a symlink to the same file is rejected before reading content

### Requirement: activation MUST be guarded by verification

Activation MUST require a completed apply and required credential imports, start egress proxy then
Manager then Monitor, and reverse-stop on failure. The installation MUST NOT be marked activated or
qualified until verify succeeds and writes bound evidence.

#### Scenario: Manager start fails

- **WHEN** egress starts but Manager fails
- **THEN** Monitor is not started and egress is stopped
- **THEN** receipt remains non-activated and verify reports failure

### Requirement: installed generated surfaces MUST be attested

Verify MUST mechanically enumerate all units, shim, polkit, gitconfig and toolchain wrappers from the
generator inventory. Missing or functionally different installed content MUST fail; comment-only drift
MUST warn. Evidence MUST bind receipt, candidate, plan, installed artifact and service identity hashes.

#### Scenario: functional unit drift

- **WHEN** an installed unit loses one non-comment `ReadWritePaths` line
- **THEN** verification fails even if file owner and mode still match

### Requirement: rollback MUST be bounded by the receipt

Rollback MUST only reverse receipt-recorded assets whose current hash matches the transaction output,
restore prior metadata/ACL/content when recorded, retain unknown/durable state, and never recursively
delete unenumerated paths.

#### Scenario: post-install unknown file exists

- **WHEN** an unknown file appears below a durable-state directory after installation
- **THEN** rollback leaves it in place and reports it as retained unknown state
