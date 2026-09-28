# Execution qualification lifecycle

`cortex model qualification` publishes profile-bound evaluation evidence through a separate lifecycle. A PatchMUD result, a profile key, or `cortex model profile --apply` is not approval by itself.

## Evidence and state

The Trust Root `execution-qualification` tree is itself a registered, installer-managed asset (so every child directory has an unambiguous managed parent) and contains these durable records:

- `candidates/`: immutable, digest-checked report/profile candidates.
- `operator-receipts/`: immutable, content-addressed receipts created by the confirmed operator CLI. The directory is writable by Operator and Manager; job principals cannot write it.
- `operator-receipt-index.json`: a Manager-only auxiliary listing (writers/readers are Manager-only in the published Trust Root ACL). Operator identity never writes or reads it — issuing and validating a live operator receipt never depends on this file existing or being reachable.
- `receipts/`: immutable lifecycle approval, test review, and revocation receipts.
- `index.json`: the authoritative lifecycle revision, current generation, idempotency records, and clock watermarks.
- `approved-roster.json`: a rebuildable projection; it is not the lifecycle authority.

Import accepts PatchMUD report schema v2 and checks its fingerprint, producer revision, artifact digest, exact resolved profile key, report/runtime role mapping, executor/model identity, cohort, per-run dimensions, and encounter coverage. A profile binding is parsed through the existing #835 schema core. Unknown observed conditions remain unknown; requested conditions never fill observation gaps. A different model, adapter/version, effort/profile key, loadout, role, deck, or coverage cannot borrow a neighboring row.

The operator receipt binds the immutable candidate, candidate/report/profile digests, exact profile key, role, coverage, policy revision, actor/reviewer, reason, reviewed time, expiry, **the lifecycle binding's generation at issuance**, and a digest derived from its canonical contents. The receipt file itself is the authority: its id is the hash of its own canonical contents (minus the id field) and its digest covers the full payload, so any copied, forged, or hand-edited file fails identity or digest verification on its own, independent of any separate registry. Publishing and every roster query re-read the receipt file directly and recheck the candidate binding; a caller-supplied string, altered file, digest mismatch, or a receipt bound to a generation the binding has since moved past is rejected. When `operator-receipt-index.json` happens to be reachable and records a matching id, its digest/candidate_id/verdict are cross-checked as an extra corroboration layer — but a missing, unreadable, or not-yet-populated index (the expected state when the Operator account cannot reach a Manager-only asset) never blocks a genuine operator receipt, and never substitutes for the file-level checks. Approval is refused unless the report passes, coverage is complete, the exact resolved profile has a complete actual-condition key, and the target role is not marked unknown. A test-only candidate requires a test-only receipt and is excluded from sized-dispatch enforcement and the default legacy parser projection.

Every candidate/model/profile/role binding carries a monotonic generation counter that advances on every approve, reject, **and revoke**. Because the operator receipt's identity (and its content digest) is derived over the generation observed at issuance, re-running `qualification approve` with byte-identical actor/reason/policy-revision/reviewed-at/expires-at after a revoke never returns the receipt issued before that revoke — it is bound to a superseded generation, so issuing again always produces distinct new evidence. Symmetrically, replaying a receipt that was valid for an earlier generation (for example the approval receipt from before a revoke) is rejected as bound to a superseded generation and can never flip a revoked or expired binding back to `approved`; only a fresh receipt issued against the binding's current generation can re-admit it.

Every compared timestamp is parsed as timezone-aware ISO-8601 and normalized to UTC. Naive or malformed timestamps fail closed. Import, review, revoke, and migration use the current `--expected-revision` and an `--idempotency-key`; stale revisions conflict and exact replays return the original result. `review_candidate`/`revoke_qualification` re-measure the current time again after acquiring the lifecycle lock and immediately before any write, so a request that held the lock queue long enough for a receipt to expire is judged against the real time at write, not a snapshot taken before the lock wait. Revocation and expiry block new admission. Query clock rollback, missing/tampered receipts, invalid roster projections, and unknown state fail closed. Clock-rollback protection is watermark-based: once any query or write has observed a time at or after a qualification's `expires_at`, a later query with an earlier clock fails closed. A host clock that is set back before any such observation is recorded is partially covered (#1097): each query also reads the latest on-disk timestamp of two Trust-Root-registered, Manager-only durable stores — the job/slice/workflow-run registry (`jobs.json`, rewritten on almost every dispatch/status transition) and the append-only `quota-admission-decisions` receipts (writer restricted to the Manager principal) — via a single bounded `stat()` per source, and treats their latest mtime as a floor on `now` (never lowering it). If that floor has already crossed `expires_at`, the query fails closed with `reason="clock-evidence-expired"` even though the caller-supplied (rolled-back) `now` has not. When neither source exists, is unreadable, or is not a regular file, the query behaves exactly as before #1097 and the result carries a `clock_evidence` field set to `"clock-evidence-unavailable"`, making the residual, fully-uncovered case (no qualification observation *and* no activity in either durable source between the real expiry and the rollback) explicit rather than silent; closing that remaining gap still needs a trusted external time source and is not claimed as covered here. Legacy roster entries are preserved as provenance with state `unknown`; migration never promotes them.

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

Approve an exact candidate through the operator entry. It records `--actor` as the reviewer, requires a reason, and prompts for confirmation on a terminal. Automation must pass `--yes` explicitly:

```bash
cortex model qualification approve <candidate-id> \
  --actor <operator-id> --reason <review-reason> \
  --policy-revision <policy-revision> \
  --reviewed-at <timestamp-with-timezone> --expires-at <timestamp-with-timezone> \
  --expected-revision <revision> --idempotency-key <stable-request-id> --yes
```

`qualification review` is reserved for explicit `--test-only` lifecycle fixtures; it cannot create a live approval. To revoke, use `qualification revoke <candidate-id>` with `--actor`, `--reason`, `--policy-revision`, `--revoked-at`, `--expires-at`, `--expected-revision`, and `--idempotency-key`; it also prompts unless `--yes` is set. Query one exact runtime identity with `qualification status --executor ... --model-id ... --profile-key ... --role ...`. `qualification migrate-legacy <roster-file>` preserves old rows as unknown and also uses revision CAS.

The operator CLI writes only the durable receipt file under the Operator/Manager-writable `operator-receipts/` directory, then publishes the lifecycle receipt from that same file; it never needs to write the separate Manager-only `operator-receipt-index.json` (an Operator account and a Manager account can be entirely separate principals under the published Trust Root ACL). `--actor` is the audited reviewer label; authority to issue comes from the governed operator/Manager entry and its Trust Root write permissions. Issue, plan, agent review, and test-only receipt are not substitutes for approval of the exact qualification.

## Dispatch and compatibility

The host overlay option `qualification_policy.sized_dispatch: enforce` enables the manager query for sized work. With the default `disabled` policy, Manager does not query qualification and dispatch behavior stays unchanged. Under enforcement, the query returns only an approved, unexpired, non-revoked live record with the exact `profile_key`, runtime `role`, complete coverage, and matching lifecycle/operator receipt digests. Test-only receipts and legacy `Identity.execution_qualification` attributes are not consulted.

Qualification state lives in independent files and does not add keys to nested workflow rows consumed by older Manager/Monitor versions. The legacy `EvalRosterEntry` parser is reused only for the roster projection; its closed row schema remains unchanged. New profile `--apply` behavior remains behind its existing human review gate and does not create or approve qualification receipts.

## Live acceptance still required

The repository fixtures demonstrate the v2 consumer and fail-closed paths: the checked-in report does not cover its full deck, and the checked-in observed profile has unknown conditions and a different resolved key. A production positive query therefore requires a producer report and profile binding that share the exact key and complete coverage, plus a valid human receipt under the active policy. A controlled live check must verify the source revision/digests, approve the exact candidate through the governing human authority, query the resulting roster under `sized_dispatch: enforce`, confirm test-only receipt rejection, and confirm revoke/expiry block a subsequent admission. No benchmark or live approval is performed by the local test suite.
