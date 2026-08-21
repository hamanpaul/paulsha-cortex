---
status: accepted
work_item: phase2-718-runtime-instance-authority
issue: 718
---

# Runtime instance authority repair

## Observed production failure

The Phase 2 deployment launches the slice `phase2-plan-manager-gitconfig-763`
as systemd instance `phase2-plan-manager-gitconfig-763-50f62414`. The builder
successfully published `commits.bundle` in that exact slot. The durable job row,
however, has the internal registry id `phase2-plan-manager-gitconfig-763-132`.
`job_workspace.spool_key_for_job()` re-derives the consumer slot from that
different id, so harvest silently returns `None` and the source branch remains
at the dispatch base.

## Required change

1. Persist the exact Manager-issued template instance from `LaunchHandle` into
   the durable job row. Treat it as typed launch authority, not payload text.
2. Make spool consumers use that persisted instance byte-for-byte. Validate it
   with the canonical safe instance predicate and fail closed if malformed.
3. Preserve the legacy/direct fallback only for jobs that genuinely predate the
   new typed field. Do not infer the instance from `session_name`, log paths,
   worktree names, payload text, or by re-hashing a different registry id.
4. Keep canonical workflow, legacy slice, gate, commit-bundle, and retry paths
   on the same derivation helper.
5. Add regression coverage that reproduces the production shape: raw slice id,
   hashed systemd instance, suffixed internal job row id, and bundle located
   only in the real instance slot. Assert harvest reaches the candidate and no
   sibling/foreign slot is consulted.
6. Add a changelog fragment referencing #718. Make the minimum necessary diff.

## Verification

Run at least:

```text
python3 -m pytest -q tests/test_bundle_commit_harvest_623.py tests/test_trust_root_isolation_718.py tests/test_coordinator_launcher.py
python3 -m policy_check --repo .
```

Commit all intended changes with a Conventional Commit and leave the worktree
clean. Do not widen ACLs and do not touch installed runtime state.

## Review adjudication: first candidate rejected

Candidate `25f6db48a68e9af3219c8319d84bb5087888ab60` is rejected. It persists the
correct concrete instance, but then passes that value back through
`canonical_job_slot()`, whose `template_instance_id()` transformation is not
idempotent. Production creates:

```text
raw phase2-plan-manager-gitconfig-763
  -> phase2-plan-manager-gitconfig-763-50f62414
```

The rejected consumer resolves:

```text
phase2-plan-manager-gitconfig-763-50f62414
  -> phase2-plan-manager-gitconfig-763-50f62414-61200d73
```

Therefore producer and consumer still differ. The new test hid the defect by
creating its fixture with the already-normalized instance, so both fixture and
consumer double-normalized it.

Repair requirements:

1. Keep raw job-id-to-instance derivation and exact-instance path resolution as
   separate, explicitly named APIs. Never make normalization heuristically
   idempotent; a raw id that happens to look hashed must remain unambiguous.
2. A persisted `template_instance` must be joined to the registered writable
   root byte-for-byte after canonical instance validation, with no second hash.
3. Production-shape tests must create the producer slot from the raw slice id,
   then consume it from the persisted concrete instance and assert the absolute
   paths are exactly equal. Also assert the double-hashed sibling is absent and
   untouched.
4. Audit both commit-bundle and gate spool consumers for this distinction.
5. Remove or fix any first-candidate test that proves only double-hash parity.
