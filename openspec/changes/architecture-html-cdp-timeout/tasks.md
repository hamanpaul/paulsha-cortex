## 1. Upstream Archify diagnostic and recovery

- [ ] 1.1 Add a typed Target.getTargets timeout record with elapsed time, attempt, Chrome process state, bounded stderr, and pinned engine identity.
- [ ] 1.2 Add one same-process retry after one second for only the first Target.getTargets timeout.
- [ ] 1.3 Add fake-CDP tests for recovery, exhaustion, late response, process/pipe error, other method timeout, and visual assertion failure.

## 2. Cortex workflow integration

- [ ] 2.1 Update the immutable Archify checkout pin after the reviewed upstream commit is available.
- [ ] 2.2 Record allowlisted runner/image/resource context, Node version, Archify ref, and ARCHIFY_CHROME path/version.
- [ ] 2.3 Preserve JSON and command logs in the existing always-uploaded artifact while retaining the required exit code.

## 3. Verification and delivery

- [ ] 3.1 Run existing architecture tests and upstream targeted visual-check tests.
- [ ] 3.2 Run the actual Architecture HTML workflow for recovered and exhausted/content-failure cases; verify browser-review and capture evidence.
- [ ] 3.3 Run strict OpenSpec, applicable documentation checks, and PR-context policy; report implementation, merge, install, and deployment separately.
