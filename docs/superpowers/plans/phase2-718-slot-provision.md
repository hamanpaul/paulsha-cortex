---
status: accepted
work_item: trust-root-isolation
issue: 718
scope_excludes:
  - executor-toolchain
  - egress-allowlist
  - phase-3-signing
---

# #718 per-job Copilot OAuth home

The downgraded live Copilot CLI 1.0.80 is already authenticated through the
operator's OAuth config, but the template job's `HOME=/var/lib/cortex-builder`
is root-owned and not writable. With no per-job `COPILOT_HOME`, the job cannot
read the operator login or create its session database. Broad `GH_TOKEN` /
`GITHUB_TOKEN` injection is not acceptable for the downgraded trust boundary.

Make one minimal Conventional Commit referencing `#718`. Change only:

- `paulsha_cortex/coordinator/spool_slot.py`
- `paulsha_cortex/coordinator/launcher.py`
- `tests/test_trust_root_isolation_718.py`
- `tests/test_coordinator_launcher.py`

Repair exactly:

1. Add one Manager-selected environment setting naming the canonical Copilot
   OAuth `config.json` authority. For a downgraded Copilot launch, require an
   absolute, readable, non-symlink regular file owned by root or the Manager
   identity, with no group/other write bits. Missing/malformed authority must
   fail before spec write / `systemctl start`, without printing credential
   bytes.
2. Inside the already-canonical per-job builder runtime-cache slot, create a
   private `copilot` home, atomically copy the authority to `config.json`, and
   grant only that job account the access needed to read the copy and write its
   own session/cache files. Do not make the canonical authority job-writable and
   do not share a writable home between jobs.
3. Put exact `COPILOT_HOME=<per-job-cache>/copilot` and
   `COPILOT_AUTO_UPDATE=false` in the Manager-owned job spec. Assert that
   `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_TOKEN` are absent from a
   downgraded Copilot job even when present in the Manager process environment.
4. Preserve the existing direct-mode token normalization for backward
   compatibility; the no-broad-token rule applies to downgraded template jobs.
   Do not log, checksum, or snapshot OAuth bytes.

Add tests for exact per-job isolation, source ownership/mode/symlink rejection,
pre-start failure ordering, and token absence. Run the trust-root and launcher
tests, commit all changes including this plan, and leave the worktree clean.

## Follow-up gate evidence

Independent pytest after commit `7a1acf2` has exactly two failures. Both use the
shared OAuth authority fixture, whose `config.json` is left group-writable by
the test process umask. The product correctly rejects it at the
`no group/other write bits` guard. Lock the valid fixture to mode `0600` (or an
equally strict explicit mode) and keep the negative mode test group-writable on
purpose. Do not weaken `copilot_oauth_authority()`. Re-run both focused test
files, commit the follow-up, and leave the worktree clean.
