---
status: accepted
work_item: monitor-canonical-todo-qualification
owner_issue: 1063
parent_issue: 1054
domain_breadth: 1
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
  - R-22
---

# Tasks

- [ ] 1. Add shared Monitor scanner path classification and strict existing-safe-file validation.
- [ ] 2. Add versioned TodoQualification and TasksValidation records to WorkSource serialization.
- [ ] 3. Parse canonical Todo metadata and Tasks from the same bytes used for source revision.
- [ ] 4. Join candidate issue/work_item/path ownership in correlation and expose qualified_todos through WorkAuthority.
- [ ] 5. Enforce generic kind:path existence/scanner validation before atomic override mutation; preserve spec/design/plan links.
- [ ] 6. Add parser, correlation, WorkAuthority, serialization, and link non-mutation fixtures for every #1063 acceptance case.
- [ ] 7. Update CLI help, README, lifecycle docs, changelog fragment, and Unreleased entry for the implementation PR.
- [ ] 8. Run focused and required repository gates, strict OpenSpec validation, PR-context policy, and diff review.
