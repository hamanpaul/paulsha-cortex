---
status: accepted
work_item: trust-root-copilot-toolchain-pinning
---

# Trust-root Copilot toolchain pinning Design

## Decisions

- Generate an immutable root-owned wrapper from the exact toolchain manifest;
  resolve the real executable before launch and verify its hash/version.
- Keep operator and job toolchains separate and make every mismatch visible in
  the receipt and attestation.
