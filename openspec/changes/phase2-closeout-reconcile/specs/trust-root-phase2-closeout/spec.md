## ADDED Requirements

### Requirement: Executable shebang drift MUST fail attestation

Generated-vs-installed attestation MUST treat the leading `#!` line of `shim` and
`toolchain_wrappers` artifacts as functional content. A changed or missing interpreter
MUST produce `functional_drift`, even when the remaining body and metadata match.

#### Scenario: installed wrapper uses another interpreter

- **WHEN** an installed shim or toolchain wrapper has a different shebang from its generated artifact
- **THEN** attestation MUST fail with `functional_drift` for that exact artifact
- **THEN** the expected shebang MUST appear in `missing_functional_lines`

### Requirement: Polkit standalone comments MUST remain non-functional

Generated-vs-installed attestation MUST ignore standalone JavaScript `//` comments and
standalone `/* ... */` comment blocks for `polkit` artifacts. It MUST NOT ignore inline
rule content or apply this JavaScript comment grammar to unrelated categories. An unterminated
block comment MUST fail closed as malformed functional content. A `;`-prefixed polkit line MUST
remain functional because JavaScript treats the semicolon as a statement, not a comment marker.

#### Scenario: only local polkit comments differ

- **WHEN** installed polkit content differs only by standalone JavaScript comments
- **THEN** attestation MUST remain successful
- **THEN** it MUST emit `comment_only_drift` for that exact artifact

#### Scenario: installed polkit block comment is unterminated

- **WHEN** installed polkit content begins `/*` without a closing `*/` before EOF
- **THEN** attestation MUST fail with `functional_drift` for that exact artifact
- **THEN** it MUST NOT downgrade the malformed content to `comment_only_drift`

#### Scenario: installed polkit rule is prefixed by a semicolon

- **WHEN** installed polkit content adds a `;polkit.addRule(...)` line
- **THEN** attestation MUST fail with `functional_drift` for that exact artifact
- **THEN** it MUST NOT apply systemd/gitconfig semicolon-comment semantics to polkit

### Requirement: Superseded work MUST be reconciled by capability evidence

Closeout MUST compare the intended capability of #681/#695/#716 with the latest shipped
call path. It MUST NOT merge an older implementation solely because its historic module or
test filename is absent. Rejected legacy implementations and their current replacements
MUST be recorded with risk and rollback evidence.

#### Scenario: newer architecture satisfies an old implementation goal

- **WHEN** the latest tree provides an equivalent or stronger canonical capability
- **THEN** the old branch MUST remain closed and MUST NOT create a parallel authority
- **THEN** work-item, Todo, and issue records MUST point to the canonical replacement

### Requirement: Package release and live deployment canary MUST remain distinct

The package release MUST be unlocked only by deterministic, credential-free, exact-SHA
release qualification. Provider/model/Manager GitHub/full intake-to-closeout health MUST
remain an independent protected deployment-canary acceptance and MUST NOT block a package
whose source and deterministic release gates pass.

#### Scenario: source is released but no live canary exists

- **WHEN** exact-main RC and GitHub Release succeed without a same-SHA deployment canary
- **THEN** Phase 2 source/package MAY be marked shipped
- **THEN** current production/provider health MUST remain explicitly unverified

### Requirement: Deployment canary MUST observe the exact Codex agent loop

The protected deployment canary MUST invoke production intake with an exact run-scoped
`codex/gpt-5.3-codex-spark` builder override. Closeout MUST bind the resolved workflow identity,
typed build-job runtime, and Manager-owned template job spec. It MUST accept command observation
only from the unique `worktree-isolation` job and only when Codex JSONL contains a completed
`command_execution` with exit code zero and non-empty output. Uploaded evidence MUST contain only
bounded identifiers, counts, booleans, and SHA-256 digests, never raw commands or output.

The job-writable JSONL is observational telemetry and MUST NOT be represented as an independent
authority. A shipped implementation without a successful protected run MUST leave #716 open and
live health unverified.

#### Scenario: another build card emits a successful command

- **WHEN** a successful command event exists only in a build job whose card is not `worktree-isolation`
- **THEN** deployment-canary closeout MUST fail
- **THEN** the event MUST NOT satisfy the agent-loop marker

#### Scenario: Manager launch identity drifts

- **WHEN** the workflow override, resolved identity, runtime metadata, template spec command,
  worktree, log path, or model differs from the exact contract
- **THEN** deployment-canary closeout MUST fail before publishing passed evidence
