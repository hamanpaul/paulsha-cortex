---
status: accepted
work_item: trust-root-agent-loop-qualification
---

# Trust-root real agent-loop qualification Todo

Issue: `hamanpaul/paulsha-cortex#716`.

## Boundary

Close the gap between a scripted sandbox probe and the production-shaped
model-driven executor loop. The qualification must exercise the actual
`codex exec`/configured agent command shape and keep the chosen outer
hardening plus egress controls intact. It must not switch to an unsafe mode or
declare success from a probe that bypasses the failing path.

## Tasks

- [ ] Reproduce the failing real agent loop under the exact generated systemd
      unit, recording command, sandbox profile, child process, and exit reason.
- [ ] Implement the smallest contract correction that lets the intended job
      loop run while preserving the outer enforcement plane and explicit
      egress allowlist; document the policy trade-off if an inner sandbox is
      intentionally absent.
- [ ] Add production-shaped positive and negative tests for model-driven
      repository commands, child processes, forbidden paths, network hosts,
      and no-unsafe-fallback behavior.
- [ ] Bind qualification evidence to executor/model identity, unit hash,
      candidate SHA, and artifact hashes; SKIP, fallback, quota, and model
      mismatch are failures.
- [ ] Run focused tests, full pytest, policy/preflight, independent review,
      delivery, CI, and close the issue through Cortex.
