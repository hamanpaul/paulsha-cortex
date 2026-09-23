---
status: accepted
work_item: delivery-journal-conditional-commit
issue: 983
parent: 963
---

# Delivery Journal Conditional Commit Design (#983)

## Current source trace

`work_actions._load_runs` reads and validates the v1 full-file snapshot. `_save_runs` writes a temporary file, fsyncs it, replaces the journal, and fsyncs the directory, but neither locks nor compares a baseline. Twenty-one `work_actions.py` call sites and two `work_bridge.py` call sites use this pair; all ignore `_save_runs`'s return value. `_load_work_run` may save once when creating a run row and again for provenance. Ship may save several transitions from the same in-memory state. A new API that protects only #980's append while leaving ordinary `_save_runs` as an unconditional replacement would still allow those writers to erase #980 intent/result. Returning a conflict value from `_save_runs` would likewise let existing callers proceed to a side effect.

## Decisions

### D1 — One locked, revisioned journal primitive in `work_actions.py`

Use a permanent sibling lock file, not a lock on the replaceable journal inode. Open it without following a symlink and verify a regular file. Hold an exclusive process lock across load, exact baseline check, immutable-entry validation, atomic replace, fsync, and confirmation read. Never unlink the lock file. All Manager writes reached through `_save_runs`, including the existing `work_bridge.py` callers, use this one boundary. Keep lock acquisition bounded and surface lock failure as a failure, not as permission for an unlocked write.

Add an optional monotonic top-level revision to the v1 journal; revisionless legacy input is revision zero. `_load_runs` exposes a baseline token alongside the ordinary mutable mapping without persisting the token inside `runs`. The token binds existence, revision, and exact content digest. `_save_runs` requires the token and refreshes it only after a confirmed commit, allowing existing code to mutate and save the same loaded state repeatedly. A raw newly constructed mapping without a loaded baseline is not accepted as an unconditional overwrite. If a call site copies or detaches the mapping, it must carry an explicit validated baseline through the narrow API. Tests must inspect all production call sites for bypasses.

### D2 — Check full-file freshness, then retain every committed row

Under the lock, re-read and validate the current journal. Compare its existence, revision, and exact file digest to the caller's baseline. On mismatch, classify conflict before replacement. Do not automatically overlay the caller's changed run on the newer file: a row change can depend on source authority, ship phase, or another run's state. The caller must reload and recompute. Ordinary `_save_runs` validates that the proposed payload preserves every existing publication entry and adds none; it rejects removal, replacement, malformed/result-only entries, and direct additions. Event equality uses canonical event payload bytes and their digest, not incidental whole-file JSON whitespace. A successful changed write increments revision exactly once. An exact replay through the narrow append API may return committed without another revision increment.

### D3 — Generic append-only intent/result operation

Provide a narrow run-keyed event append operation in the same module. Its storage envelope contains an opaque intent payload and optional opaque result payload, each with canonical digest and one stable event identity. #980 owns the meaning of those payload fields; this child enforces only exact identity, intent-before-result, immutable retention, and same-payload replay. The append operation alone may add an entry. It evaluates against a fresh locked journal, so unrelated run rows can be retained without a stale caller copying them. A different canonical payload at the same event identity is a conflict even when a caller claims that it is a retry. Legacy rows lacking this envelope load with an empty event set; they are never backfilled into producer proof.

### D4 — Durability and uncertain outcome

Write a validated complete candidate to a unique same-directory temporary file, fsync it, atomically replace the journal, and fsync the parent directory. Confirm the exact revision and event/row after replacement while the lock is still held. The narrow append API reports `committed` only then. A mismatch before replacement is `conflict`. Any error after the transaction begins whose outcome cannot be proven is `unknown`; it must stop the caller. The compatibility `_save_runs` wrapper keeps its existing success return and raises on conflict, unknown, or invalid state, since its callers do not inspect return values. On restart, load under the same lock and compare the exact event identity and canonical payload hash. If present and equal, treat it as committed replay; if absent, a new conditional attempt may proceed; if different, conflict. Mere file visibility before directory sync is never reported as durable confirmation.

### D5 — Scope and failure posture

The implementation changes only `work_actions.py` in production. No #978 registry or receipt logic and no #982 GitHub client logic enter this writer. #980 must receive committed intent before PR create and committed result before registry append. Conflict and unknown from ordinary saves raise to the Manager before push, review, or merge; the narrow append result must be explicitly checked by #980. #983 itself does not mint a receipt or modify remote PRs. Tests use independent processes/instances plus injected filesystem failures, and include the two existing `work_bridge.py` save paths as consumers of the same protected writer.

## Verification boundary

An accepted packet and source tests establish only a candidate contract. They do not prove #980 provenance, #847 drift equivalence, merged or loaded runtime, or the AC10 live canary. If integration requires changing `work_bridge.py` or another production module for this child, rerun sizing before implementation rather than retaining the one-module score.
