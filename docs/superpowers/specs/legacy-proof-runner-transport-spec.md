---
status: accepted
work_item: legacy-proof-runner-transport
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# G — fixed Builder runner 規格

## Authority and scope

Provision a narrowly callable root-owned systemd service/broker that runs only F under cortex-builder and returns an invocation-bound trusted proof transcript.
Blocked by F and E. Uses the existing controlled cortex install service lifecycle; no Manager direct traversal.

## Requirements

- **R1** — Install/remove a fixed root-owned unit through cortex install service; execute only the installed proof helper as cortex-builder in a read-only namespace for the exact requested workspace.
- **R2** — Bind request ID, nonce, exact path, expected tuple, actual unit UID/properties, systemd InvocationID, helper output and digest in a trusted result; reject caller-created or cross-request transcript.
- **R3** — Manager can submit only a constrained typed request and read a protected result while actual Manager UID remains unable to traverse or write the original private 0700 clone.
- **R4** — Reject arbitrary executable/shell/path roots, symlink/replay/truncation, UID or InvocationID mismatch, nonce/digest mismatch and service failure; never fall back to Manager git commands.
- **R5** — Test with installed service and distinct Manager/Builder UIDs against real .git directory and file workspaces; same-UID tests or mocked systemd metadata do not pass.
- **R6** — Missing host permissions, unit properties or trusted transport leaves AC7 blocked; no chmod/ACL workaround.

## Acceptance and verification

- [ ] Install/remove a fixed root-owned unit through cortex install service; execute only the installed proof helper as cortex-builder in a read-only namespace for the exact requested workspace.
- [ ] Bind request ID, nonce, exact path, expected tuple, actual unit UID/properties, systemd InvocationID, helper output and digest in a trusted result; reject caller-created or cross-request transcript.
- [ ] Manager can submit only a constrained typed request and read a protected result while actual Manager UID remains unable to traverse or write the original private 0700 clone.
- [ ] Reject arbitrary executable/shell/path roots, symlink/replay/truncation, UID or InvocationID mismatch, nonce/digest mismatch and service failure; never fall back to Manager git commands.
- [ ] Test with installed service and distinct Manager/Builder UIDs against real .git directory and file workspaces; same-UID tests or mocked systemd metadata do not pass.
- [ ] Missing host permissions, unit properties or trusted transport leaves AC7 blocked; no chmod/ACL workaround.

## Non-goals

No proof schema ownership (F), WorkAuthority/OS operator authorization (H), registry transaction, identity migration, recovery adapter or cleanup policy.

## Verification boundary

此三件套 status=accepted 只代表 planning scope 已寫完整，不代表外部 issue owner acceptance、產品實作、測試、CI、merge 或 #547 closure。
