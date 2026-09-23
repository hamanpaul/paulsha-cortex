---
status: accepted
work_item: legacy-checkpoint-contract
issue: TBD
domain_breadth: 0
state_consistency: 1
invariant_count: 5
artifact_classes:
  - documentation
---

# E — checkpoint contract 規格

## Authority and scope

Freeze one implementable contract for extending the existing #862 checkpoint writer. The live #862 issue describes a single absent-to-complete registry transaction and complete receipt; it does not yet promise #968 identity fields, Work Item lineage, or a workspace verifier.
Requires explicit #862 owner acceptance and the #968 A1/#969 marker contracts; it also consumes #971's WorkAuthority/action boundary.

## Requirements

- **R1** — #862 owner explicitly accepts the exact payload, transaction boundary, exact target selection, failure semantics, and existing complete receipt type; record an authoritative contract revision/reference.
- **R2** — One transaction reselects and revalidates exact slice, job, and WorkflowRun fingerprints/cardinality plus immutable repo/work_id lineage; external prefilter followed by label first-match is forbidden.
- **R3** — Contract defines identities, migration record, proof digest/observed_at, operator provenance, request ID and canonical full digest. Same-CAS success writes all required records and the existing complete receipt together.
- **R4** — Manager checks for an existing complete receipt before obtaining fresh proof; absent receipt gathers all evidence, freezes digest, and invokes one CAS. Replay returns the stored receipt without new proof/time. No prepared phase, partial pin, second receipt, or second writer.
- **R5** — If #862 owner refuses any required atomicity/reselection or replay contract, downstream AC7 implementation stays blocked and old rows stay unbound.

## Canonical input payload contract

E freezes the versioned payload that J accepts; I consumes this contract and must not redefine it. The payload is a typed immutable envelope with these required fields:

```text
schema_version
request_id
work_authority: { repository_identity, owner_work_key }
proposed_identity: { slice_identity, worktree_identity }
expected_target: { slice_fingerprint, job_fingerprint, workflow_run_fingerprint }
operator: { os_uid, principal_id, authorization_policy_revision }
workspace_proof: { verifier_id, invocation_id, nonce, observed_at,
                   workspace_path, git_top_level, git_dir_kind,
                   repository_identity, head, branch, marker_status,
                   helper_version, proof_digest }
payload_digest: { algorithm, value }
```

All fingerprints are canonical digests of exact persisted rows and are selectors only when the transaction reselects and compares the rows. `work_authority` and `proposed_identity` are exact normalized values, never display labels. `workspace_proof` is the invocation-bound read-only result from G/F; it proves current repository/path/Git facts, not historical Work Item ownership. The transaction independently revalidates persisted slice→job→WorkflowRun lineage and immutable run repo/work_id. `payload_digest` covers every preceding field, including fresh `observed_at`, proof digest, request ID, authorization provenance, and all three fingerprints; all values are frozen before the single CAS call. `requested_by` may be retained separately for audit but is never authentication input.

## Acceptance and verification

- [ ] #862 owner explicitly accepts the exact payload, transaction boundary, exact target selection, failure semantics, and existing complete receipt type; record an authoritative contract revision/reference.
- [ ] One transaction reselects and revalidates exact slice, job, and WorkflowRun fingerprints/cardinality plus immutable repo/work_id lineage; external prefilter followed by label first-match is forbidden.
- [ ] Contract defines identities, migration record, proof digest/observed_at, operator provenance, request ID and canonical full digest. Same-CAS success writes all required records and the existing complete receipt together.
- [ ] Manager checks for an existing complete receipt before obtaining fresh proof; absent receipt gathers all evidence, freezes digest, and invokes one CAS. Replay returns the stored receipt without new proof/time. No prepared phase, partial pin, second receipt, or second writer.
- [ ] If #862 owner refuses any required atomicity/reselection or replay contract, downstream AC7 implementation stays blocked and old rows stay unbound.
- [ ] J consumes the exact E payload schema above; I emits this schema and cannot add fields or change digest semantics without revised E/#862 owner acceptance.

## Non-goals

No product code, registry mutation, proof generation, operator auth, migration action, or legacy row conversion. No change to proposal-first gc.

## Verification boundary

此三件套 status=accepted 只代表 planning scope 已寫完整，不代表外部 issue owner acceptance、產品實作、測試、CI、merge 或 #547 closure。
