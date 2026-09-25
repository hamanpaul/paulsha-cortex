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

# Monitor canonical Todo qualification design

## Decisions

1. Parse each Todo from one safe byte snapshot and bind its SHA-256 source revision to a versioned TodoQualification record. Use a fixed canonical path parser for qualified Todo; keep the generic supported-scanner predicate wider for path links.
2. Evaluate the source-level candidate in correlation only after the confirmed WorkItem group is known. Qualification requires explicit same-work-item path ownership, exact work_item, and issue membership in that same repo/group.
3. Validate Tasks with Tasks-v1: one exact second-level Tasks section, unique T-number IDs, checkbox rows with nonempty title/action fields, no standalone placeholder values, and at least one pending entry. Reject the whole Tasks result on any malformed task row.
4. Project only qualified records to WorkAuthority.qualified_todos after checking identity and revision against the corresponding WorkSource. Preserve mapped_todo_paths and confirmed_todo for existing readers; #1054 must consume qualified_todos.
5. Share one path validator between Monitor correlation and work link mutation. For kind:path, require a canonical repo-relative supported scanner path, existing regular target, no symlink traversal, and repo containment before atomic override write. This does not inspect Todo metadata or Tasks and keeps spec/design/plan links working.
6. Keep parsing rejection source-local with reason codes. Preserve existing provider last-good handling for I/O failure. Do not promote a link write into a freshness claim; #1064/#1065 own generation and WorkAuthority freshness.

## Observable invariants

- Every qualified record's path and revision equal its WorkSource ref and revision.
- Its issue belongs to WorkAuthority.mapped_issues and its work_item equals WorkAuthority.work_id.
- Generic path-link eligibility and Todo qualification are separate predicates.
- Invalid path link attempts perform zero override writes.
- Qualified Todo output is a derived read result; no new mutable store, claim writer, generation, or Manager gate is introduced.
