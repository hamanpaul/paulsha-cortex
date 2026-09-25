---
status: accepted
work_item: delivery-journal-conditional-commit
issue: 983
parent: 963
---

# Delivery Journal Conditional Commit Specification (#983)

## Requirements

### R1 — One serializable Manager write boundary

Every production Manager write of `delivery-journal.json`, including writes reached through `work_bridge.py`, MUST pass through one `work_actions.py` conditional-commit boundary. The boundary MUST serialize cooperating processes on a stable lock identity independent of the journal inode replaced by `os.replace`. Reading the current journal, checking the caller's exact baseline, validating the proposed update, replacing the file, syncing file and parent directory, and reporting success MUST be inside that protected boundary. A stale writer MUST NOT overwrite another committed run row, including a row for a different run.

### R2 — Exact baseline and conflict result

The load operation MUST return the journal content with an opaque baseline token that binds file existence, a monotonic persisted journal revision, and the exact bytes/content read. Legacy v1 journals without a revision start at revision zero and remain readable. Each successful changed commit advances the persisted revision; an exact no-op may return the already committed result. Comparison by mtime, inode, row count, or run ID alone is insufficient. A stale baseline or malformed current journal MUST fail closed with an explicit conflict/invalid outcome. A conflict requires a fresh load and recomputation of the intended mutation; it MUST NOT silently merge a stale full-file snapshot or assume that another run's row can be rebuilt.

### R3 — Immutable publication event entries

The same boundary MUST expose a narrow append operation for an opaque, run-keyed publication intent or result entry. It binds run ID, event identity, entry kind, and the exact canonical payload hash; it does not interpret a PR or planning receipt. An intent entry MUST be committed before its result entry. Existing entries MUST retain identical canonical event payload bytes; unrelated whole-file JSON whitespace is not event identity. Replaying the same event identity, kind, and payload is an idempotent no-op; the same identity with a changed payload/hash, a result without its committed intent, removal, or replacement is a conflict. Ordinary full-file `_save_runs` MUST preserve existing publication entries and reject any new, removed, replaced, or malformed publication entry, including result-only entries; only the narrow append operation may add an entry. A caller cannot obtain a committed result by supplying only an in-memory row or by observing a file after an uncertain write.

### R4 — Three outcome semantics across crash boundaries

The narrow append API MUST distinguish `committed`, `conflict`, and `unknown`. `committed` means the exact update completed file fsync, atomic replacement, parent-directory fsync, and a fresh exact read/validation while protected by the lock. `conflict` means no mutation was attempted because current durable state was inconsistent with the caller's baseline or immutable event. `unknown` means an I/O error/crash boundary may have left the replacement visible but durability cannot be confirmed. The existing `_save_runs` callers ignore its return value, so `_save_runs` MUST preserve its success convention and raise on conflict, unknown, or invalid state; it MUST never return a non-success value that lets a caller proceed to push, review, or merge. A narrow append caller MUST likewise stop on conflict or unknown. Neither outcome authorizes #980 receipt minting. On retry after an unknown outcome, a fresh locked read resolves whether the exact event committed, conflicts, or remains absent; never replay a stale full-file snapshot.

### R5 — Compatibility and owner boundary

Existing `cortex-delivery-journal/v1` run rows remain readable. Existing non-publication fields and rows survive all commits. The only production module modified by this child is `paulsha_cortex/coordinator/work_actions.py`; tests and documentation may change. This child supplies journal write mechanics and generic immutable event storage. It does not define #978 receipt schema, #982 PR creation provenance, #980 PR intent contents, #979 planning transaction journal, registry CAS, PR side effects, or authority drift equivalence. If a required production change crosses this module boundary or coordinates another store, recalculate sizing and return to scope review.

## Acceptance

- [ ] Two processes load the same baseline and propose different run rows: at most one stale-snapshot commit succeeds; the other reports conflict, and a fresh reader still sees every confirmed row. An explicit fresh-load retry can preserve both rows.
- [ ] The same run's exact intent and result append/replay are idempotent; changed identity, canonical payload/hash, removed entry, result-before-intent, and stale snapshot fail closed. Ordinary `_save_runs` cannot add or forge a publication entry.
- [ ] Existing production `work_actions.py` and `work_bridge.py` call sites use the protected writer without changing `work_bridge.py` in this child; a direct old-style stale save cannot bypass the baseline check, and conflict/unknown raises before its caller's next push, review, or merge side effect.
- [ ] Fault injection around temp-file fsync, replacement, directory fsync, read-back, and persist-then-raise yields only committed, conflict, or unknown as specified. A fresh locked reader resolves an uncertain exact event without overwriting other rows.
- [ ] Legacy revisionless journals, missing journal, malformed journal, and duplicate/conflicting event entries are handled explicitly. Confirmed rows and event entries survive fresh process reload.
- [ ] Tests cover distinct processes or independent writer instances, stale reload, same and different run concurrency, crash/replay, and mutation-catching negative controls; no test treats a visible file or an exception as a receipt.

## Dependencies and delivery accounting

#983 is a producer-side prerequisite of #980 under #963/#847. #978 owns the receipt contract and authenticated read-back; #982 owns safe PR creation attribution. #980 consumes this journal operation for its durable PR intent/result. This planning packet does not deliver product code, a receipt, a PR, merge, installed runtime, or #847 AC06/AC10 completion.
