# Governed merge risks and rollback

## Rejected source risks

- **#789 behavioral/atomicity conflict:** its temporary tree is not guaranteed to share the
  destination filesystem, and rename into an existing non-empty tree is not idempotent. Importing
  it would bypass the current plan/receipt transaction.
- **#790 authority conflict:** a `permgen` inventory would duplicate the install plan's canonical
  generated inventory and permit disagreement between install and verify.
- **#791 interface/evidence conflict:** its prompt prescribes commands and its producer never parses
  command events; a direct standalone probe could pass without production lifecycle or autonomous
  agent-loop evidence.

## Selected-change risks

- **Over-normalization:** ignoring too much polkit text could hide functional drift. Mitigation:
  JavaScript comments are recognized only for the `polkit` category; code remaining after a leading
  block comment remains functional; an unterminated block fails closed; regression coverage includes
  a changed inline rule, an EOF-malformed block, and a semicolon-prefixed JavaScript rule.
- **Cross-category false negative:** treating all `#`/`;` prefixes as generic comments hid shell or
  polkit code. Mitigation: each artifact category has an explicit grammar; shebang/semicolon and
  polkit hash/semicolon regressions lock the fail-closed cases.
- **Live-status ambiguity:** a package release could be mistaken for provider health. Mitigation:
  #716 remains open and explicitly requires a protected same-SHA canary; release never consumes
  canary inputs or evidence.
- **Observational log trust:** the builder owns its JSONL log, so a parsed command event is not an
  independent attestation. Mitigation: byte-bind the complete Manager spec, require exact HEAD output,
  verify model/effort/provider/cwd through provider-persisted thread metadata, cross-bind external
  work identity and unique log/artifact-set hashes, upload no raw command/output, and keep #716 open
  until the protected canary itself succeeds.
- **Upgrade authority discontinuity:** a new plan cannot claim existing v0.1.9 objects without proof.
  Mitigation: accept only an explicit applied/qualified prior receipt for the same install topology,
  verify every filesystem kind and venv link before mutation, persist adoption in the new journal,
  reject exact-looking toolchain/venv state without receipt provenance, serialize every receipt
  operation with one host-global transaction lock, hold a separate host-global maintenance lease
  across the runbook service lifecycle, and reject mismatches before backend mutation.
  A receipt-proven toolchain whose desired bytes change at the same path is also rejected up front;
  supported upgrades allocate a new versioned path because the backend has no atomic in-place replace.
  The runbook creates a unique effective receipt path under the canonical receipt parent for every
  invocation. Before reporting ready or stopping services, the root helper proves that path absent
  and durably records it with the exact service pre-state in root-private state. Mutations require
  the lease's plan-bound token, so abort can fully roll back only its own receipt, including the
  signal-after-apply window. A dead helper leaves a plan/token marker that admits only the original
  token; a whole-shell crash loses that token and therefore requires explicit exact-plan recovery,
  which rotates stale authority, stops current Cortex units, rolls back the snapshot receipt, and
  restores only the previously-active units when rollback is proven safe. Recovery failure preserves
  the snapshot and marker across reboot. The reviewed plan is atomically published to root-only
  durable storage before lease acquisition, so fresh-shell recovery verifies the exact prior bytes
  instead of trying to recreate ingress/venv that correctly reject overwrite. Snapshot publication
  itself is complete-before-final-name. Venv staging uses receipt-bound planned/building/ready inode
  and tree authority, while mount adoption carries original inode authority across metadata-only
  upgrades. The sealed root-owned candidate tree is built only after manifest=actual
  wheelhouse validation and hash-required/no-deps installation, then re-hashed before plan/apply. A separate
  root/admin writer can ignore an advisory lock and is explicitly outside the job-account threat
  model; per-step reinspection then fails closed with durable rollback authority rather than a false
  zero-mutation claim.
- **Incomplete release ingress:** a qualified wheel without its bundle-referenced source/toolchain
  and canonical install config cannot replay the production runbook. Mitigation: retain the complete
  qualification input in the RC artifact, revalidate its exact topology and hashes at release time,
  byte-compare the regenerated install config, and publish one deterministic install-input archive
  plus the permanent passed qualification manifest beside the exact qualified wheel. The release
  transaction checks GitHub's REST digest for all three assets and rolls back its owned tag/release
  on ordinary failure, INT, or TERM. A durable marker in the annotated tag lets a subsequent run
  remove only its exact-SHA owned stale draft/tag after hard kill; foreign and non-draft releases
  remain fail-closed boundaries.
- **Legacy reference CLI:** `permgen.build_toolchain_plan()` remains as a compatibility/reference
  surface but is not the install authority. Any removal or deprecation is a separate change; this
  closeout does not strengthen it or route production install through it.

## Rollback

Before merge, discard only the isolated feature worktree. After merge, revert the closeout merge
commit; do not rewrite `v0.1.9` or any later tag. A production install, if separately authorized,
must use `cortex install trust-root rollback --receipt <exact receipt>` so only receipt-owned,
unchanged state is restored; this closeout itself performs no production installation.
