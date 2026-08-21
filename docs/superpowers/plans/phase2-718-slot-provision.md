---
status: accepted
work_item: trust-root-isolation
issue: 718
scope_excludes:
  - log-slot-identity
  - copilot-runtime-toolchain
  - phase-3-signing
---

# #718 pre-start typed surface provisioning

After commit `6d9a18d`, log identity is fixed in source. The remaining live
failure is that a template unit's generated `ReadWritePaths=.../%i` includes the
hashed monitor event-spool row, but the launcher creates only Codex-home/cache
and specialized commit/log rows. PID 1 then fails at `226/NAMESPACE` before the
model starts.

Make one minimal Conventional Commit referencing `#718`. Change only:

- `paulsha_cortex/coordinator/spool_slot.py`
- `paulsha_cortex/coordinator/launcher.py` only if call/error context needs it
- `tests/test_trust_root_isolation_718.py`
- `tests/test_coordinator_launcher.py` only for the pre-start ordering contract

Repair exactly:

1. Before `systemctl start`, enumerate the canonical
   `PER_JOB_WRITABLE_SURFACES` rows matching the selected principal and create
   or validate every `%i` slot required by that template unit. Use the existing
   typed table and `canonical_job_slot`; do not add a second list.
2. Specialized consumers may subsequently reset/preseed their own commit, log,
   or verdict slot. A missing, symlinked, or malformed row must fail in Manager
   before `systemctl start` with the surface id and exact path.
3. Preserve ACL semantics. Codex-home/runtime-cache keep their current explicit
   readable+writable projection. Commit, event, verdict, and job-log rows must
   inherit their deployment-installed write-only/default ACL; do not grant
   read access, add a default ACL, widen a parent, or add a writable root.
4. Preserve idempotence for an already-valid slot and the no-cross-mount-link
   design.

Add tests proving every builder row exists before mocked `systemctl start`, a
malformed event row prevents start with actionable diagnostics, and write-only
rows do not pass through the runtime-cache ACL widening path. Run the focused
trust-root and launcher tests, commit all changes including this plan, and leave
the worktree clean.
