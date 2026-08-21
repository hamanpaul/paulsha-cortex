# trust-root-isolation

## Task 1: Freeze the per-job writable-surface contract with failing tests

- [x] Add tests that enumerate every job-writable spool surface from one canonical table and fail if generator, provisioner, run-under properties, or probe coverage omits a row.
- [x] Add two-instance contract tests covering commit spool, monitor event spool, review-verdict spool, gate-ledger spool, and gate worktree; own and foreign slot behavior is specified.
- [x] Assert concrete `%i` slot rendering, writable-root exclusion, fail-closed missing identity, and rejection of unsafe slot shapes.
- [x] Run the focused spool, workspace, job-runner, gate, and trust-root permgen tests red before implementation, then preserve their exact failing assertions as regression gates.

## Task 2: Implement canonical slot derivation and provisioning

- [x] Extend the trust-root surface table and derive deployment, provisioning, run-under, and probe output.
- [x] Update producers/consumers and cleanup behavior to use the canonical owned slot.

## Task 3: Make Codex control inputs immutable without breaking refresh

- [ ] Add immutable-control smoke coverage for config, plugins, skills, and hooks while preserving auth refresh.
- [x] Pass `--ignore-user-config` on every `codex exec` lane and update the byte-pinned argv regression tests.
