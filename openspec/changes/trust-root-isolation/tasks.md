# trust-root-isolation

## Task 1: Freeze the per-job writable-surface contract with failing tests

- [x] Add tests that enumerate every job-writable spool surface from one canonical table and verify canonical path and probe projection for every row.
- [x] Add source tests covering the five concrete producer/consumer slot paths and their shared systemd instance basename.
- [x] Assert concrete `%i` slot rendering, writable-root exclusion, fail-closed missing identity, and rejection of unsafe slot shapes.
- [x] Run the focused spool, workspace, job-runner, gate, and trust-root permgen tests red before implementation, then preserve their exact failing assertions as regression gates.

## Task 2: Implement canonical slot derivation and provisioning

- [x] Extend the single structured trust-root surface table with real R1 runtime-root assets and derive production template-unit, permission-plan, replica/run-under, canonical path, provisioning ACL, and probe output from it.
- [x] Update commit, event, verdict, ledger, and gate-worktree producers/consumers to use the digest-bearing systemd instance slot selected from Manager job identity.
- [ ] Deploy generated units and run two-principal own/foreign byte-identity probes; this remains Manager/operator work and is not source-test evidence.

## Task 3: Make Codex control inputs immutable without breaking refresh

- [x] Add source-level immutable-control and fail-closed canonical projection coverage for config, plugins, skills, and hooks; add the Manager-owned auth seed/atomic-commit contract for sequential jobs.
- [x] Register deployment-owned control and Manager-owned credential authorities; migrate only the four control leaves with normalized readable modes, stripped source metadata, and atomic create-only installation; generate one owner-aware post-stop named-Manager-ACL publication contract for builder/reviewer auth refreshes before harvest, with unchanged Manager-owned seeds as a safe no-op.
- [x] Pass `--ignore-user-config` on every `codex exec` lane and update the byte-pinned argv regression tests.
- [x] Route oversized Claude/CG workflow prompts through a bounded Manager-created per-job prompt file; isolated template/transient launch paths keep prompt bytes out of argv, env, and status, and Manager exit accounting cleans the file after termination.
- [x] Keep the auth publisher owner-aware for unchanged Manager-owned seeds and atomic job-owned refreshes, and add source/regression coverage for the split-UID protocol without changing the live deployment claim.
- [x] Add explicit Codex reasoning-effort argv pins for `gpt-5.6-luna` (`max`) and `gpt-5.3-codex-spark` (`xhigh`).

## Task 4: Bind event production to isolated slots

- [x] Bind the production hook default and `--spool-root` CLI path to authoritative `PSC_JOB_ID`, and make the monitor harvest one-level job slots without following symlinks.
- [x] Derive isolated Codex credential harvest from typed launch/runtime surface metadata; missing slot, authority, identity, or harvest now persists a runtime diagnostic and cannot be classified as provider failure.
- [x] Use the canonical surface row ACL recipe for runtime slot/control projection and copy only regular files/directories with normalized modes and atomic publication; retain live own/foreign probes as pending.
- [ ] Run the live two-job persistence, normal Codex start, auth-refresh write, and generated-vs-installed probes under deployed hardening.
