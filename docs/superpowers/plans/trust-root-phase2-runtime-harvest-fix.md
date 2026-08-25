---
status: accepted
work_item: trust-root-phase2-runtime-harvest-fix
issue: 718
---

# Trust-root Phase 2 exact template-instance credential harvest

## Observed failure

The deployed normal-Codex builder turn can complete successfully, but
`Dispatcher._finalize_headless()` currently re-derives the runtime slot from
the registry job id. Template jobs have two different identities:

1. the raw slice id used by the launcher to create the systemd template
   instance and provision runtime surfaces; and
2. the suffixed durable registry job id used for bookkeeping.

Re-hashing identity (2) looks for a sibling slot that was never provisioned.
The finalizer then records `runtime-credential-harvest-failed` even though the
Codex turn itself succeeded. This is a Phase 2 exact-instance authority bug,
not a provider failure.

## Required change

1. Keep raw job-id derivation and exact template-instance joining as separate,
   explicitly named APIs. Do not make the raw hash helper heuristically
   idempotent.
2. Add an exact-instance credential harvest path in
   `paulsha_cortex/coordinator/spool_slot.py` (or an equivalent typed helper)
   that validates the persisted instance and joins it with
   `exact_job_slot(...)` without a second hash. Preserve the existing raw
   `job_id` API for genuinely direct/legacy callers.
3. Change `Dispatcher._finalize_headless()` so a `systemd-template` job uses
   the persisted `template_instance` / `job_workspace.spool_key_for_job(job)`
   as the authority. Missing or malformed template authority must fail closed;
   it must never fall back to the suffixed registry id. Direct/legacy jobs may
   retain the raw-id fallback.
4. Keep the source-level runtime identity contract and the installed runtime
   behavior aligned; do not edit credentials, generated units, or service
   state by hand.

## Regression evidence

Add or extend focused tests (prefer `tests/test_trust_root_isolation_718.py`)
that create a temporary credential authority and two identities:

- a raw slice id (derive its real template instance with the production helper),
- a durable registry id with a different suffix.

Provision and write the refresh payload only in the exact template-instance
slot. Assert that exact-instance harvest updates the authority, the
double-hashed/suffixed sibling is not consulted, and malformed or missing
template authority fails closed. Retain coverage for the existing raw-id
direct/legacy helper.

## Verification

Run the focused runtime/trust-root tests, policy check, and the repository's
normal full test command appropriate for this docs-and-code change. Commit all
intended changes with a Conventional Commit, include the changelog fragment,
and leave the builder worktree clean. Do not claim the live Phase 2 task from
the builder; the Manager must deploy the candidate and re-run the final
normal-Codex slice before checking it.
