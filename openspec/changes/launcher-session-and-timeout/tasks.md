---
status: accepted
work_item: launcher-session-and-timeout
---

# Pre-archive tasks

- [x] T02: Record the five `_ARGV_BUILDERS` across direct, `systemd-run`, and
  `systemd-template`; all 14 legal cells assert `start_new_session=True`, while
  `cg`/template retains the unknown hardening-profile rejection before `Popen`.
- [x] T03: Keep the shared launcher helper as the production source of the
  session flag and verify that every admitted `SubprocessLauncher` launch
  consumes it after runner-specific argv/I/O overrides.
- [x] T04: Preserve the narrow Claude `stdin` compatibility retry; both
  attempts retain the session flag, non-stdin `TypeError` is not retried, and a
  second stdin failure is propagated.
- [x] T05: Preserve the fixed fake-Popen signatures and existing argv/env/cwd/
  stdin/accounting oracles while extending session recording to both systemd
  wrapper branches.
- [x] T06: Use the production helper for a real POSIX `bash -c 'exec sleep 30'`
  fixture; verify PGID/SID ownership before the intentional group signal and
  reap only the owned process handle in bounded cleanup.
- [x] T07: Add isolated negative controls for helper bypass, a single
  runner/executor flag drop, retry flag loss, swallowed non-stdin errors, and a
  missing-session fixture; the missing-session path records zero `killpg` calls.
- [x] T08a: Run the focused launcher/session regression file and retain the
  green result for this candidate.
- [x] T08b: Run the authoritative full pytest preflight for this candidate;
  it completed with 5682 passed, 44 skipped, and 173 subtests passed.
- [x] T09: Document the session-versus-cgroup, daemon-restart, #824 timeout,
  and #851 probe boundaries in the README and lifecycle guide; use existing
  CLI help surfaces only.
- [x] T10: Preserve and extend the #823 Unreleased entry and changelog
  fragment without changing VERSION or claiming downstream delivery.
- [ ] Manager-owned policy, independent review, and freeze checks remain
  pending; their results are not represented as complete here.
- [ ] Checkout-outside installation and any merge, issue, service, or live
  runtime validation remain pending downstream actions.
