---
status: accepted
work_item: generated-asset-attestation
---

# Generated Asset Attestation Design

## Decisions

- Use one canonical renderer for desired-state assets and one root-owned,
  hash-bound inventory for the installed projection; verification compares the
  two before activation.
- Keep credential values out of generated files, logs, receipts, and JSON;
  record only provider/principal metadata, modes, and hashes.
- Treat missing or functionally mismatched assets as fail-closed rather than
  silently repairing the runtime.
