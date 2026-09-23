---
status: accepted
work_item: main-sync-builder-m-object-import
---

# Builder clone exact-M import 設計（#973 B1b）

## Decisions

### D1 Transfer at provision time
The Builder clone is independent and may not contain M. Inside the existing per-job provisioning transaction, run an explicit local-path Git fetch from the Manager source repository using B1a's exact advertised pin as the only source ref and the Builder private main-sync ref as the only destination. Use `--no-tags`; do not depend on clone defaults or network credentials. This runs before ACL/Builder execution.

### D2 Keep M separate from the branch base
The clone's checked-out feature branch and base ref remain C. Create a namespaced private ref for M; do not move HEAD, feature, origin/main or the existing BASE_REF. B1c points its exact merge command at this private ref.

### D3 Roll back incomplete provisioning
If the imported ref is absent/wrong or verification fails, remove the newly created clone through existing rollback and return failure before dispatch. Do not leave a partially provisioned Builder workspace or acknowledge B1a cleanup.

## Sequence

1. Clone the exact feature branch/base C as today.
2. Read source repository path and exact job-scoped M pin from the Manager handoff.
3. Validate token/job/C/M; explicitly fetch only the pinned source ref into the Builder private ref.
4. Verify source pin and Builder private ref both resolve M, `HEAD==C`, and feature/base refs remain C.
5. Acknowledge to Manager, then let normal permissions/launch proceed.

## Sizing boundary

Only `seams.py` is changed; local clone provisioning has one private ref and no remote or WorkflowRun transaction. domain=0/state=1 gives 5 / Yellow with complete accepted planning.
