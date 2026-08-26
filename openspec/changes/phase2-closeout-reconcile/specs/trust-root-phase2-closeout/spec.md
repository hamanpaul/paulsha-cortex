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
rule content or apply this JavaScript comment grammar to unrelated categories.

#### Scenario: only local polkit comments differ

- **WHEN** installed polkit content differs only by standalone JavaScript comments
- **THEN** attestation MUST remain successful
- **THEN** it MUST emit `comment_only_drift` for that exact artifact

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
