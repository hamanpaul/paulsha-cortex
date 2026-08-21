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
- [x] Register deployment-owned control and Manager-owned credential authorities; migrate only the four control leaves with normalized readable modes and atomic create-only installation; generate post-stop named-Manager-ACL publication for builder/reviewer auth refreshes before harvest.
- [x] Pass `--ignore-user-config` on every `codex exec` lane and update the byte-pinned argv regression tests.

## Task 4: Bind event production to isolated slots

- [x] Bind the production hook default and `--spool-root` CLI path to authoritative `PSC_JOB_ID`, and make the monitor harvest one-level job slots without following symlinks.
- [ ] Run the live two-job persistence, normal Codex start, auth-refresh write, and generated-vs-installed probes under deployed hardening.
