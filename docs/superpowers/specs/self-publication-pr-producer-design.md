---
status: accepted
work_item: self-publication-pr-producer
issue: 980
parent: 963
---
# Manager PR Publication Producer Design (#980)

## Decisions

### D1 — Respect A/B/C and source ownership

#980 changes only `work_bridge.py`. It consumes #992's `manager_pull_request` closed receipt union and canonical identity rules, #993's one-shot private registry append and full-file revision CAS, #994's authenticated read-only PR observation, #982's structured create/adopt outcome, and #983's conditional delivery-journal writes. #980 creates/retains the immutable intent sidecar required by #992 and coordinates its reference with #983. It does not reimplement any shared encoder, schema, registry CAS, client API, or GET validator.

The order is #994 first, #982 second because both modify `github_delivery.py`, then #980's `work_bridge.py` producer wiring. #980 is blocked by #992/#993/#994/#982/#983, but it does not wait for #978 aggregate merge. #979 is a separate planning producer; #964/#965 remain downstream consumers/status/canary owners.

### D2 — Canonicalize request and intent with #992's formulas

Represent request metadata as `{title, body, labels}`. Normalize title to NFC; normalize body only by CRLF/CR → LF while preserving all other code points; normalize labels to NFC, sort, and deduplicate. Reject marker-free bodies containing the casefold sentinel `cortex-self-publication-intent:`. Compute `request_metadata_digest = H("cortex-pr-request-metadata/v1", request_metadata)` using #992's exact `J` and `H` definitions.

The immutable intent core binds repo/work/run/claim, base and head repositories/branches, the 40-hex candidate commit SHA, and request-metadata digest, matching #992/#980. It excludes `ship_step_card` and ship attempt; those belong in #992's separate producer-event basis `H("cortex-self-publication-event/manager-pr/v1", exact run/claim/ship-step/attempt/candidate basis)`. `publication_id = H("cortex-manager-pr-intent/v1", immutable_intent_core)`. The core also excludes the publication ID, marker, PR number, read-back facts, result, and mutable phase.

Render body exactly per #994: normalized nonempty body without trailing LF gets one LF; empty body or body already ending in LF gets no extra separator; then append one standalone `<!-- cortex-self-publication-intent:v1:<publication_id> -->` line. The marker is public correlation data, never creator proof.

### D3 — Retain sidecar and conditionally journal before POST

Write immutable intent bytes exactly as `J(intent_core)` at `intent_ref`; compute `intent_sha256` over the exact raw bytes and retain the sidecar. The #992 transaction refers to it with the exact closed `kind`, `intent_ref`, and `intent_sha256` keys. #983 stores the same reference and Manager operation state using its protected conditional write. Only `committed` plus a fresh exact read-back permits the create call. Journal file visibility alone is not a durability witness.

After #982 returns a complete successful POST witness `{repository, number, id, node_id}`, use #983 to commit that witness under the same intent; verify committed status and reload before any #994 GET. After an exact #994 observation, persist the observation under the same immutable event with #983 and verify it before receipt append. Any conflict, unknown, persist-then-raise, or stale state stops; do not write from `_load_runs()` snapshots or treat a local file reread as commit proof. A crash with a sidecar but no journal row may resume only by recomputing the same intent and proving exact sidecar equality before idempotently establishing the #983 row; no remote request happened before that row was committed.

### D4 — Require #982 creation attribution and #994 exact observation

The #982 structured result is `created`, `adopted`, or `ambiguous`. Only a successful POST with exact repo, positive number, positive REST ID, and nonempty node ID is a create witness. Adoption, incomplete response, timeout, or lost response remains non-minting. A matching public marker cannot reconstruct a lost POST response.

Call #994 only after the successful witness is durable in #983. Supply its exact expected tuple and normalized request metadata; require the typed observation to match repository, number, REST ID, node ID, open state, head repo/branch/candidate SHA, base repo/branch, exact sole marker, and title/body/labels. #994 performs read-only GETs and does not establish creator proof itself.

### D5 — Append one typed receipt with #993

Construct #992's exact closed manager PR union, including its `accepted_input`, `acceptance_evidence`, `published_object`, and transaction fields. The sidecar is exact `J(intent_core)`; transaction carries `intent_ref` and `intent_sha256`. Keep candidate SHA distinct from SHA-256 evidence digest. `pre_publication_authority_sha256` is the pre-publication `work_authority_digest(WorkAuthority)`, never `source_revision`.

After #983 durably records the exact GET observation, call #993's private append once. It atomically appends `publication_receipts` and updates the coupled `WorkflowRun.pr_refs`/`source_revision` under exact registry revision CAS. Reload a fresh registry and revalidate receipt, sidecar, POST witness, and GET identity. Resolve uncertain append by exact `(producer_event_id, publication_id)` reload; never retry from stale state or mint a second event.

### D6 — Gate every existing PR route

The current `run.pr_refs` path must validate a receipt for the exact run/claim, its retained sidecar, confirmed POST witness, and matching #994 observation before metadata sync or merge-capable `_ship_action`. A legacy reference, same marker, matching branch/head, source membership, or `planning_authority` is insufficient. The Manager's canonical `_metadata_file` body includes the exact marker so marker-preserving metadata sync does not remove it; #982 must never retrofit it onto an adopted PR.

### D7 — Fail closed on uncertainty and preserve evidence

Lost/timeout POST, missing/non-durable witness, #983 conflict/unknown, malformed sidecar, #994 mismatch, or #993 conflict/failure produces no receipt. Preserve any remote PR for operator resolution. No automatic replacement create, delete, merge, receipt backfill, or conversion of a marker match to `created`. Existing and malformed receipt history remains readable but invalid per #992/#993.

## Verification boundary

Implementation tests exercise Manager ship path, all three journal durable boundaries, #994 exact metadata/object checks, #993 append/replay/reload, and the existing-`pr_refs` guard. Cover same-run immutability and two-process stale writer/persist-then-raise cases from #983, plus every crash boundary. Product tests, merged source, installed runtime, and #965/#847 live canary remain separate evidence.
