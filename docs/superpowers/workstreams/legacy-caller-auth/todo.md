---
status: accepted
work_item: legacy-caller-auth
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 5
artifact_classes:
  - source
  - tests
  - documentation
---

# H — operator authentication Tasks

## Tasks

- [ ] **T01**：Define and install the root-owned versioned UID/repo/work_id/action allow policy.
- [ ] **T02**：Safely open request and derive UID/inode identity from its descriptor; reject races and unsafe files.
- [ ] **T03**：Join principal with confirmed WorkAuthority and create immutable provenance.
- [ ] **T04**：Test allowed and denied paths, forged JSON, TOCTOU, policy permissions and replay.
- [ ] **T05**：Document that authorization does not establish historical Work Item ownership.

## Sizing and completion record

Five dimensions: verified current_sizing_snapshot output = 6 / Yellow (1/1/2/0/2) under fix-standard. Artifact refs are the sibling spec, design and todo. Record implementation/test/review/CI/merge/runtime evidence separately.

## Non-goals

No general control authentication, human login, GitHub identity, WorkAuthority synthesis, workspace proof, registry mutation or automatic migration.
