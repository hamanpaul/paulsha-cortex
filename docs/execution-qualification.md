# Execution qualification lifecycle

`cortex model qualification` publishes profile-bound evaluation evidence through a separate lifecycle. A PatchMUD result, a profile key, or `cortex model profile --apply` is not approval by itself.

## Evidence and state

The manager-owned `execution-qualification` directory contains four durable records:

- `candidates/`: immutable, digest-checked report/profile candidates.
- `receipts/`: immutable approval, rejection, and revocation receipts.
- `index.json`: the authoritative lifecycle revision, current generation, idempotency records, and clock watermarks.
- `approved-roster.json`: a rebuildable projection; it is not the lifecycle authority.

Import accepts PatchMUD report schema v2 and checks its fingerprint, producer revision, artifact digest, exact resolved profile key, report/runtime role mapping, executor/model identity, cohort, per-run dimensions, and encounter coverage. A profile binding is parsed through the existing #835 schema core. Unknown observed conditions remain unknown; requested conditions never fill observation gaps. A different model, adapter/version, effort/profile key, loadout, role, deck, or coverage cannot borrow a neighboring row.

An approval receipt is bound to the immutable candidate and its report/profile digests, exact role and coverage, policy revision, reviewer, authority reference, review time, and expiry. Approval is refused unless the report passes, coverage is complete, the exact resolved profile has a complete actual-condition key, and the target role is not marked unknown. Rejection can retain an incomplete candidate for audit. A test-only candidate requires a test-only receipt and is excluded from normal qualification queries and the default legacy parser projection.

Each import, review, revoke, and migration supplies the current `--expected-revision` and an `--idempotency-key`. Stale revisions conflict; exact replays return the original result. Revocation and expiry block new admission. Query clock rollback, malformed times, missing/tampered receipts, invalid roster projections, and unknown state fail closed. Legacy roster entries are preserved as provenance with state `unknown`; migration never promotes them.

## CLI

Inspect the current contract with `cortex model --help` and `cortex model qualification <operation> --help`.

Import report v2 and an optional #835 binding. Omitting the binding creates an unknown observation that cannot be approved:

```bash
cortex model qualification import \
  --report ./report-v2.json \
  --source-revision <producer-git-sha> \
  --profile-key epk:v1:resolved:<64-lowercase-hex> \
  --executor <executor> --model-id <model-id> --role build \
  --profile-binding ./execution-profile.json \
  --expected-revision <revision> --idempotency-key <stable-request-id>
```

Review an exact candidate. Live review requires a reference issued by the governing human approval process; test receipts require the explicit `--test-only` flag:

```bash
cortex model qualification review <candidate-id> \
  --verdict approved --reviewer <human-reviewer> \
  --authority-ref <human-receipt-reference> \
  --policy-revision <policy-revision> \
  --reviewed-at <timestamp-with-timezone> --expires-at <timestamp-with-timezone> \
  --expected-revision <revision> --idempotency-key <stable-request-id>
```

Query one exact runtime identity with `qualification status --executor ... --model-id ... --profile-key ... --role ...`. Revoke its current generation with `qualification revoke <candidate-id>`, providing the reviewer, authority reference, reason, timestamp, expected revision, and idempotency key. `qualification migrate-legacy <roster-file>` preserves old rows as unknown and also uses revision CAS.

The `--authority-ref` value is recorded as receipt provenance. This local CLI does not authenticate an interactive account or validate an external human approval service; live acceptance must verify the reference with the authority that issued it. Issue, plan, agent review, and test-only receipt are not substitutes for approval of the exact qualification.

## Dispatch and compatibility

The host overlay option `qualification_policy.sized_dispatch: enforce` enables the manager query for sized work. With the default `disabled` policy, Manager does not query qualification and dispatch behavior stays unchanged. Under enforcement, the query returns only an approved, unexpired, non-revoked record with the exact `profile_key`, runtime `role`, `coverage: complete`, and receipt digest. Legacy `Identity.execution_qualification` attributes are not consulted.

Qualification state lives in independent files and does not add keys to nested workflow rows consumed by older Manager/Monitor versions. The legacy `EvalRosterEntry` parser is reused only for the roster projection; its closed row schema remains unchanged. New profile `--apply` behavior remains behind its existing human review gate and does not create or approve qualification receipts.

## Live acceptance still required

The repository fixtures demonstrate the v2 consumer and fail-closed paths: the checked-in report does not cover its full deck, and the checked-in observed profile has unknown conditions and a different resolved key. A production positive query therefore requires a producer report and profile binding that share the exact key and complete coverage, plus a valid human receipt under the active policy. A controlled live check must verify the source revision/digests, approve the exact candidate through the governing human authority, query the resulting roster under `sized_dispatch: enforce`, confirm test-only receipt rejection, and confirm revoke/expiry block a subsequent admission. No benchmark or live approval is performed by the local test suite.
