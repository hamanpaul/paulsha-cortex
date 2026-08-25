---
status: accepted
work_item: trust-root-agent-loop-qualification
---

# Trust-root real agent-loop qualification Specification

## Requirements

- Qualification MUST run the production-shaped model-driven executor command,
  not a scripted probe that bypasses the failing path.
- The intended outer sandbox, enforcement plane, and explicit egress allowlist
  MUST remain active; unsafe fallback or model mismatch is a failure.
- Positive and negative tests MUST cover repository commands, child processes,
  forbidden paths, and forbidden hosts.
- Evidence MUST bind executor/model, unit hash, candidate SHA, and artifact
  hashes; quota refusal, fallback, or SKIP MUST fail the gate.

## Acceptance

- The exact command, profile, child tree, exit result, and redacted evidence are
  reproducible in the qualification harness.
