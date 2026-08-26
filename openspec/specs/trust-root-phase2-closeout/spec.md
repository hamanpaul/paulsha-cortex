# trust-root-phase2-closeout Specification

## Purpose
定義 Trust Root Phase 2 closeout 的 attestation、transactional upgrade、release
qualification 與 live canary authority 邊界。

## Requirements

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
A `#`-prefixed polkit line MUST also remain functional because `#` is not a JavaScript line-comment
marker. Conversely, shell shim/toolchain content MUST ignore standalone `#` comments but MUST keep
`#!` and `;`-prefixed lines as functional content.

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

#### Scenario: category comment grammar is not borrowed from another format

- **WHEN** installed polkit adds a `#`-prefixed line or an installed shell shim adds a
  `;`-prefixed line
- **THEN** attestation MUST fail with `functional_drift`
- **THEN** only the comment syntax native to that artifact category MAY be ignored

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
only from the unique `worktree-isolation` job. That first builder prompt MUST ask the model to choose
its own useful read-only repository inspection and MUST NOT prescribe the acceptance command. The
qualification driver MUST reconstruct and byte-compare the complete Manager-generated prompt and
terminal schema from the canonical workflow/card contract rather than trust fixture-authored prompt
text. The complete shell wrapper MUST byte-match the
canonical Codex argv, exit-code capture, bundle publication, last-message publication, and terminal
exit sequence. The job spec MUST bind `CODEX_HOME` to the exact registry-derived, non-symlink,
Manager-owned per-job slot, bind `PATH` to the exact installed toolchain/system path, reject every
Git repository-selector environment variable, and grant exactly one `safe.directory` equal to the
bound worktree. The provider thread probe MUST use that same slot. Codex JSONL MUST
contain one persisted thread identity and a completed whole-command `/usr/bin/git rev-parse HEAD`
invocation (optionally with exact `-C <bound-worktree>`) bound to the job worktree whose only
non-empty output is the exact `worktree-isolation` job `subject_head`. The serialized command MAY be
that direct argv or one exact three-argument `/bin/bash` or `/usr/bin/bash` `-c`/`-lc` envelope; only
one envelope MAY be removed and its inner argv MUST be exact. The app-server
`thread/resume` metadata MUST independently report the exact model,
`xhigh` effort, OpenAI provider, and bound cwd. Uploaded evidence MUST project the terminal into an
exact-key state/work-id/run-id object and otherwise contain only bounded identifiers, counts,
booleans, model identifiers, and SHA-256 digests, never the raw work-show envelope, commands, or
output. Work ID and every other uploaded identifier MUST use an explicit 128-character bound.

The Cortex release candidate SHA, the `worktree-isolation` probe-job subject SHA, and the probe
workflow's final candidate SHA MUST be recorded as distinct typed fields and bound to their own
producer evidence. Qualification MUST NOT require them to be equal: later commit-required build
cards legitimately advance the disposable probe repository after the observed job.

The independent validator MUST receive repository, work ID, and issue as external protected
workflow inputs. It MUST bind them to dispatch terminal/GitHub evidence, require exactly one builder
job observation for the `worktree-isolation` card, and bind the raw job-log digest to a unique safe
artifact row and the canonical artifact-set digest. Marker names and observed builder-job IDs MUST
be an exact bounded set/shape; extra raw command or output strings MUST be rejected. Each artifact
row MUST carry the digest of the bytes actually validated, never bytes re-read after a mutable
Manager file advances. A commit bundle MUST have the same digest before and after Git verification
and head enumeration; only that stable digest may be published. Command/output digests remain
privacy-preserving observations, not reconstructable raw data.

The job-writable JSONL is observational telemetry and MUST NOT be represented as an independent
authority. A shipped implementation without a successful protected run MUST leave #716 open and
live health unverified.

#### Scenario: another build card emits a successful command

- **WHEN** a successful command event exists only in a build job whose card is not `worktree-isolation`
- **THEN** deployment-canary closeout MUST fail
- **THEN** the event MUST NOT satisfy the agent-loop marker

#### Scenario: the canary fixture prescribes its own passing command

- **WHEN** the worktree-isolation prompt differs from the canonical autonomous prompt or names the
  acceptance command
- **THEN** qualification MUST fail before treating any command event as model autonomy evidence

#### Scenario: shell composition tries to forge the HEAD proof

- **WHEN** the command uses a repository-local `./git`, pipe, boolean fallback, redirection,
  alias, or any suffix around the expected Git operation
- **THEN** qualification MUST fail even if the event claims exit 0 and prints the exact candidate

#### Scenario: later build cards advance the probe candidate

- **WHEN** `worktree-isolation` proves its bound subject HEAD and later commit-required cards produce
  a different final workflow candidate
- **THEN** deployment-canary closeout MUST bind both identities and MAY pass
- **THEN** neither probe identity MUST be forced equal to the Cortex release candidate SHA

#### Scenario: raw terminal details are added to uploaded evidence

- **WHEN** the terminal object contains a raw command, raw output, provider observation, title,
  next action, nested detail, or any key beyond state/work ID/run ID
- **THEN** independent validation MUST fail

#### Scenario: Manager launch identity drifts

- **WHEN** the workflow override, resolved identity, runtime metadata, template spec command,
  worktree, log path, or model differs from the exact contract
- **THEN** deployment-canary closeout MUST fail before publishing passed evidence

#### Scenario: requested model differs from provider-persisted runtime

- **WHEN** the job spec requests Spark/xhigh but the persisted Codex thread reports another model,
  effort, provider, or cwd
- **THEN** deployment-canary closeout MUST fail
- **THEN** requested registry/spec metadata MUST NOT substitute for provider runtime evidence

#### Scenario: dispatch evidence names another protected work item

- **WHEN** dispatch repository, work ID, or issue differs from the validator's external canary input
- **THEN** deployment-canary validation MUST fail even if all files and hashes are self-consistent

### Requirement: Production upgrade MUST use one sealed candidate and explicit prior authority

The executable production runbook MUST use one absolute candidate CLI for plan and every privileged
installer action. That CLI MUST be installed offline from the exact hash-complete wheelhouse into a
root-owned, non-symlink, group/other-non-writable tree. The manifest wheelhouse inventory MUST equal
the actual wheelhouse inventory, and bootstrap installation MUST name every wheel through a hash-
required lock while disabling dependency discovery. Its deterministic tree digest MUST be equal
before plan and apply. Both invocations and the operator-facing plan digest/rendering MUST use a
closed trusted tool path and MUST NOT resolve an ambient `cortex`, Python, checksum utility, user
config, `PYTHONPATH`, or `$HOME` package tree. After owner/hash validation,
the ingress and sealed venv MUST be read/traverse executable by the non-root plan user without
granting group/other write. A canonical `lib64 -> lib` created by `venv --copies` MAY be removed only
after its exact target is verified; every remaining symlink MUST fail validation. Service shutdown
MUST skip absent units and restore every previously-active unit on any command failure, shell exit,
interrupt, termination, or partial stop/apply failure before apply succeeds.

An upgrade MUST explicitly pass `--prior-receipt`. The prior receipt MUST be root-owned, applied,
qualified, from a different plan, and share the exact scheme, instance, roots, and repository remote.
Only accounts and filesystem steps whose current state is proven by an exact prior step/journal MAY
inherit provenance. The new receipt MUST record `adopted_from_receipt`; incompatible authority MUST
fail before receipt state transition, backend apply, or host mutation. Before the first mutation,
the installer MUST inspect every existing asset, repository, toolchain, candidate venv slot, and
active venv link so drift in a later apply-order step cannot leave earlier steps modified. The CLI
MUST hold one non-blocking host-global transaction lock whose identity is independent of roots, plan,
and effective receipt-path override. Apply, credential import, activate, verify, and rollback MUST
share that lock. The executable runbook MUST also hold one host-global maintenance lease from before
the service snapshot through rollback/restore or successful verify and service-active checks. Each
runbook invocation MUST allocate a unique effective receipt path under the canonical receipt parent.
Before reporting the lease ready or allowing service shutdown, the privileged helper MUST prove that
path absent and MUST durably record the reviewed-plan digest, effective receipt path, present units,
and previously-active units in root-private state that survives reboot. This lets an abort fully roll
back only the receipt it creates even if a signal arrives after apply completed but before the parent
shell observed completion. An active lease MUST reject tokenless, wrong-token, and wrong-plan
mutations; only its unguessable plan-bound token MAY admit the holder's apply, credential import,
activate, verify, or rollback. If the helper dies while the invoking shell retains that token, the
durable marker MUST reject every new normal lease and tokenless mutation while the exact original
token remains authorized to finish rollback and restore. If the whole shell dies and loses the raw
token, the runbook MUST NOT recreate immutable ingress or the sealed venv. Before acquiring the lease,
it MUST atomically publish the fully-written, fsynced, operator-confirmed plan under a root-only path
derived from its digest, without overwriting a different existing object. Fresh-shell recovery MUST
re-enter the previously reviewed digest, revalidate that exact durable plan and the root-owned sealed
CLI topology, and invoke an explicit command bound to those bytes. It MUST rotate only that
inactive stale authority, stop currently-present Cortex units, roll back only the snapshot receipt,
and restore only the previously-active units after rollback proves `restore_safe=true`. Successful
normal completion or safe recovery MUST clear the durable snapshot and marker. Any plan mismatch,
rollback drift, unsafe receipt, or service-restore failure MUST leave them in place and services
fail-closed for operator resolution. Fresh installation MUST omit `--prior-receipt`.

The maintenance snapshot MUST be complete and fsynced before an atomic no-replace final-name
publication. Candidate-venv creation MUST checkpoint a deterministic staging path before mkdir,
its device/inode after durable mkdir, and its complete tree hash before final-name rename. Rollback
MUST remove only an unpublished staging inode bound by that receipt; a completed rename MUST be
replayable from the prepublication authority without treating the retained candidate slot as unknown.
Mount adoption authority MUST retain the original device/inode across metadata-only upgrades rather
than silently rebasing authority to a later same-content object.

The zero-mutation sweep guarantee assumes no independent privileged actor ignores the installer
lock; a concurrent root/admin writer can invalidate every host invariant and is outside the
job-account threat model. Every step MUST still be re-inspected immediately before use. A late
out-of-model drift MUST fail closed with a durable applying receipt from which the operator can
rollback; it MUST NOT be described as a zero-mutation failure.

#### Scenario: a later upgrade step has foreign state

- **WHEN** an earlier step matches the prior receipt but a later existing asset, repository,
  toolchain, candidate venv slot, or active venv link does not
  match either current-receipt or qualified prior-receipt provenance
- **THEN** apply MUST fail while the new receipt remains planned with an empty journal
- **THEN** the backend MUST have applied no step

#### Scenario: exact previous deployment is upgraded

- **WHEN** a qualified prior receipt proves unchanged accounts and the installed state of each
  carried filesystem step
- **THEN** apply MAY adopt those objects and update candidate-specific steps
- **THEN** the new receipt MUST retain the adoption proof for retry, rollback, and later upgrades

#### Scenario: matching foreign toolchain or candidate venv is present

- **WHEN** a fresh install observes an exact-looking toolchain or candidate venv slot without
  current/prior receipt provenance, including self-authored wheel/tree markers
- **THEN** apply MUST fail before any backend mutation
- **THEN** marker equality MUST NOT substitute for receipt-bound tree provenance

#### Scenario: a proven toolchain would require in-place replacement

- **WHEN** the installed toolchain matches the prior receipt but the current plan changes the
  desired bytes at the same managed path
- **THEN** the pre-mutation sweep MUST reject the upgrade before applying any earlier step
- **THEN** an upgrade MAY instead use a new versioned path whose leaf does not already exist

#### Scenario: concurrent plans share one host transaction authority

- **WHEN** two installer commands use different roots, plans, or effective receipt paths on one host
- **THEN** only one command MAY hold the transaction lock
- **THEN** an active maintenance lease MUST reject every mutation lacking its exact plan-bound token

#### Scenario: the helper freezes unique authority before service shutdown

- **WHEN** the runbook begins an install attempt under the canonical receipt parent
- **THEN** it MUST choose a new unpredictable effective receipt path and the helper MUST prove it absent
- **THEN** the helper MUST durably write that receipt and the exact service pre-state before reporting ready
- **THEN** a receipt collision or snapshot conflict MUST fail before stopping a unit

#### Scenario: abort owns only its invocation receipt

- **WHEN** the runbook-created receipt reaches applied but a signal arrives before the parent shell records command completion
- **THEN** abort recovery MUST roll back that receipt before restoring the previously active units

#### Scenario: maintenance helper dies during the service window

- **WHEN** the lease helper exits before an admitted mutation or abort rollback
- **THEN** the durable marker MUST reject a new lease, tokenless mutation, wrong token, and wrong plan
- **THEN** the original shell MAY finish rollback and restore only with its exact original token

#### Scenario: the entire transaction shell dies

- **WHEN** the lease helper and invoking shell die after the durable snapshot is ready
- **THEN** recovery MUST use the atomically stored prior reviewed plan and MUST NOT regenerate input or venv
- **THEN** an explicit recovery for those exact bytes MUST rotate the inactive stale authority
- **THEN** recovery MUST stop currently-present Cortex units and roll back only the snapshot receipt
- **THEN** only a safe rollback MAY restore the units recorded previously active and clear recovery state

#### Scenario: hard-crash recovery encounters drift

- **WHEN** exact-plan recovery cannot prove rollback safe or cannot restore a previously-active unit
- **THEN** recovery MUST re-stop every unit it attempted to restore and MUST NOT report success
- **THEN** the durable snapshot and maintenance marker MUST remain for operator resolution across reboot

#### Scenario: prior receipt belongs to another root or account identity

- **WHEN** the prior receipt roots/repository differ or a current account step no longer exactly
  matches the prior account step
- **THEN** apply MUST fail before invoking the backend
- **THEN** the operator MUST NOT repair the mismatch by ambient CLI or manual file replacement

### Requirement: Release MUST publish the complete qualified install input

The deterministic RC artifact MUST retain the complete `qualification-input` tree, including the
bundle, canonical install config, candidate wheel, wheelhouse, source bundle, and toolchain. Before
publication, the release gate MUST revalidate the exact input topology and every manifest hash,
reproduce the candidate wheel, regenerate the canonical install config, and byte-compare both
reproduced artifacts with the RC-qualified input. The GitHub Release MUST contain exactly one
qualified wheel, one deterministic archive of that complete install input, and one permanent copy
of the passed release qualification manifest. Before publication, the release transaction MUST
compare the REST `digest` of each uploaded asset with the SHA-256 of its local authoritative bytes.
It MUST persist an ownership marker containing run, attempt, and release SHA in the annotated tag
before upload. A later invocation MAY reconcile a hard-kill residue only when the exact tag is
annotated, targets the same release SHA, carries a structurally valid marker, and any exact-tag
release is still a draft carrying that same marker. It MUST NOT delete a lightweight, foreign,
wrong-target, or non-draft release/tag.

#### Scenario: RC artifact omits a bundle-referenced input

- **WHEN** source, toolchain, install config, wheelhouse, or another required input is absent or extra
- **THEN** release qualification MUST fail before tag or release creation

#### Scenario: exact qualified input is published

- **WHEN** the exact-main RC input and reproduced wheel pass every binding check
- **THEN** the release MUST attach one qualified wheel, one complete install-input archive, and one release qualification manifest
- **THEN** a missing, extra, wrong-size, non-uploaded, or digest-mismatched asset MUST fail closed before publication

#### Scenario: a prior release run was killed during draft upload

- **WHEN** an exact-SHA annotated tag and draft release carry the same valid durable transaction marker
- **THEN** the next release run MAY delete that owned residue and retry publication
- **THEN** a foreign marker or non-draft release MUST be retained and MUST fail closed
