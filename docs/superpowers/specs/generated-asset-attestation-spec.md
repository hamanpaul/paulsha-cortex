---
status: accepted
work_item: generated-asset-attestation
---

# Generated Asset Attestation Specification

## Requirements

- The installer MUST emit a hash-bound inventory covering all generated
  service units, shims, polkit rules, gitconfig files, the Copilot toolchain
  wrapper, and the Manager GitHub credential surfaces needed for runtime
  attestation.
- Functional generated-vs-installed drift MUST fail verification; comment-only
  drift MAY be reported as a warning.
- Attestation and regression tests MUST prove the four-way trust-root and
  missing-credential cases without exposing credential contents.

## Acceptance

- Inventory hashes, redacted evidence, focused/full gates, Docker qualification,
  review, delivery, CI, and issue closure are recorded by Cortex.
