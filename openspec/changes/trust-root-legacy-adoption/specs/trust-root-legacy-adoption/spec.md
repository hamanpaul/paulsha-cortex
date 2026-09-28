## ADDED Requirements

### Requirement: legacy_policy MUST gate adoption without changing receipt-backed behavior

The installer MUST treat `legacy_policy: reject` exactly as today: any existing object without receipt provenance fails closed, and a config carrying a `legacy_adoption` block MUST be rejected. `legacy_policy: quarantine` without a `legacy_adoption` block MUST behave exactly as today. Only `legacy_policy: quarantine` together with a `legacy_adoption` block bound to a legacy inventory MAY adopt objects that lack receipt provenance. A plan without a `legacy_adoption` block MUST produce byte-identical plan documents and plan digests to the pre-change installer.

#### Scenario: release qualification config is unaffected

- **WHEN** the release install config (`legacy_policy: quarantine`, no `legacy_adoption`) is planned against the same bundle before and after this change
- **THEN** both plan documents and `plan_sha256` values are identical

#### Scenario: reject policy refuses an adoption block

- **WHEN** a config with `legacy_policy: reject` also carries a `legacy_adoption` block
- **THEN** planning fails with `InstallPlanError` before any output is written

### Requirement: legacy inventory MUST be a read-only, root-captured, self-digested record

`cortex install trust-root legacy inventory` MUST run as root under the shared installer transaction lock and MUST NOT mutate the host. It MUST refuse to run when a canonical receipt parent, maintenance snapshot or lease marker already exists, because such hosts must upgrade with `--prior-receipt`. The output MUST be canonical JSON carrying its own digest over stable fields only, a host binding derived from a hash of the machine identity (never the raw identity), and a scope digest derived from the config, overlay and plan-managed path set. Credential-class objects MUST be recorded by metadata only; their contents MUST NOT be read, hashed or emitted.

#### Scenario: inventory on a receipt-managed host

- **WHEN** `legacy inventory` runs on a host where the canonical receipt parent exists
- **THEN** it exits non-zero, writes nothing, and names `--prior-receipt` as the supported path

#### Scenario: credential files are metadata-only

- **WHEN** the inventory covers a provider credential destination
- **THEN** the record contains type, owner, mode and inode identity only
- **THEN** no content bytes or content digest of that file appear in the inventory

### Requirement: host overlay MUST be an explicit allowlisted delta

`--host-overlay` MUST only override account and group ids, the egress service account home, `operator_account`, `external_reader_account`, `providers.builder` and the `legacy_adoption` block. Any other key MUST fail planning. The plan MUST record the overlay digest. Account uids and gids MUST NOT be remapped by the installer; an account whose observed fields differ from the overlay, or whose uid or gid is held by a non-cortex identity, MUST fail planning.

#### Scenario: overlay tries to change a root path

- **WHEN** the overlay sets `roots.state`
- **THEN** planning fails and names the disallowed key

#### Scenario: legacy uid preserved

- **WHEN** the overlay declares the host's existing uid and gid for every cortex account and the inventory matches them field by field
- **THEN** the account steps are classified `adopt` and apply performs no account mutation

### Requirement: plan MUST derive a deterministic disposition for every inventoried object

With `--legacy-inventory`, planning MUST verify the inventory schema, self digest, scope and host binding, then derive exactly one disposition per object: `adopt`, `adopt-in-place`, `quarantine-then-create`, `quarantine`, or planning failure. Any object that no rule classifies MUST fail planning as `unclassified`. State adoption MUST be limited to the necessary subset: plan-managed state directories and their existing contents are adopted in place; state-root entries not declared by the plan, unmanaged subdirectories and leftover temporary files inside managed directories, job worktree pools, the source repository and credential-class objects MUST be quarantined. Existing file ACLs inside adopted directories MUST NOT be rewritten recursively.

#### Scenario: unmanaged state entry

- **WHEN** the state root contains a top-level directory that no plan step declares
- **THEN** its disposition is `quarantine`
- **THEN** the plan lists it in the quarantine set shown to the operator

#### Scenario: authoritative object the installer did not generate

- **WHEN** the systemd root contains a cortex unit drop-in that no plan step generates
- **THEN** its disposition is `quarantine`, never `adopt`

#### Scenario: unclassified object

- **WHEN** the inventory contains an authoritative-surface object that matches no disposition rule
- **THEN** planning fails with `unclassified` and names the path

### Requirement: legacy-quarantine steps MUST move, never delete or overwrite

A `legacy-quarantine` step MUST verify the source against the inventoried type, owner, mode, device, inode and content or tree digest, create the destination parent chain as root-only, require the destination to be on the same filesystem, and move the source with `renameat2(RENAME_NOREPLACE)`. A cross-filesystem move MUST fail closed without a copy fallback. An existing destination MUST fail closed. Replay after interruption MUST complete the step only when the source is gone and the destination inode equals the recorded prior inode.

#### Scenario: destination already exists

- **WHEN** the quarantine destination path already exists at apply time
- **THEN** the step fails closed and the source is untouched

#### Scenario: interrupted after rename

- **WHEN** apply is interrupted after the rename but before the entry is marked completed, and apply is rerun with the same plan
- **THEN** the step is marked completed without a second move

### Requirement: apply MUST re-capture the inventory and fail closed on drift

`apply --legacy-inventory` MUST be mutually exclusive with `--prior-receipt` and MUST be required when the plan carries a `legacy_adoption` block. After services are stopped and before the receipt leaves the planned state, apply MUST re-capture the inventory; the stable-field digest, host binding and scope MUST equal the plan. The writable census MUST show no job-account writable path outside plan-declared writable assets and the plan's census exceptions. In-flight jobs, active cortex services or template instances MUST fail closed.

#### Scenario: object changed between plan and apply

- **WHEN** an inventoried unit file changes after planning
- **THEN** apply fails before the first mutation with the receipt still planned and an empty journal

#### Scenario: new writable path

- **WHEN** a job account can write a path that is neither a declared writable asset nor a census exception
- **THEN** apply fails before the first mutation and names the principal and path

### Requirement: receipts MUST record legacy provenance distinguishably

The receipt MUST record the inventory digest, host binding, quarantine root and the apply-time inventory digest. Adopted entries MUST carry `adoption.source = "legacy-inventory"` and the inventory row digest; they MUST NOT be marked `adopted_from_receipt`. A later upgrade MUST be able to use an applied and qualified adoption receipt as `--prior-receipt`, with provenance proven through the recorded adoption rows.

#### Scenario: upgrade after adoption

- **WHEN** an adoption receipt is applied and qualified, and a newer candidate is planned with the same persisted host overlay
- **THEN** prior-receipt handoff succeeds and adopted accounts and directories are proven by their adoption rows

### Requirement: rollback MUST restore the legacy host and prove it

Rollback of a receipt with a legacy block MUST reverse the journal: delete receipt-created objects, restore prior directory metadata, move quarantined objects back with `RENAME_NOREPLACE`, and run `systemctl daemon-reload` when any systemd path changed. It MUST then re-capture the inventory under the plan scope and report `legacy_restored=true` only when the stable-field digest equals the original. `restore_safe` MUST be false otherwise, and services MUST remain stopped for operator decision.

#### Scenario: clean rollback

- **WHEN** apply fails after quarantining units and the operator rolls back
- **THEN** every quarantined object is back at its original path with its original inode
- **THEN** the re-captured inventory digest equals the original and `legacy_restored` is true

#### Scenario: original path reoccupied

- **WHEN** a quarantined object's original path is occupied at rollback time
- **THEN** the object stays in quarantine, the path is reported as retained drift, and `restore_safe` is false

### Requirement: quarantine purge MUST wait for a successor qualified receipt

Quarantined legacy objects MUST NOT be removed by apply, activate, verify or rollback. A purge command MAY remove them only after a later receipt for the same installation is applied and qualified and at least 30 days have passed since that qualification.

#### Scenario: purge too early

- **WHEN** purge is requested within 30 days of the successor receipt's qualification
- **THEN** it refuses and removes nothing

### Requirement: RC qualification MUST cover adoption when installer code changes

The RC qualification pipeline MUST provide a `legacy-adoption` profile that seeds a Phase 2b–shaped legacy host in the disposable container, then runs inventory, plan, apply, activate, verify and rollback, and checks the rollback proof. A release whose diff touches `paulsha_cortex/trust_root/install/` or `qualification/` MUST NOT publish without a passing `legacy-adoption` run for the exact release SHA.

#### Scenario: installer change without adoption qualification

- **WHEN** a release candidate changes installer code and no passing `legacy-adoption` run exists for its exact SHA
- **THEN** the release preflight fails and names the missing profile
