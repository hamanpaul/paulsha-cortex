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

# unified-work-read-model delta

## ADDED Requirements

### Requirement: Canonical Todo qualification is typed and bound to its WorkAuthority

Monitor MUST qualify a countable Todo only when the source path is exactly `docs/superpowers/workstreams/<slug>/todo.md`, the source has a valid issue and work_item frontmatter, its path is explicitly owned by the same repo/work item, its issue is a confirmed issue of that WorkAuthority, and its Tasks section passes the versioned Tasks-v1 format. The qualification result MUST include source ID, path, source revision, issue, work_item, task validation, status, and reason codes. WorkAuthority MUST expose only passed results through qualified_todos. Existing raw mapped_todo_paths and confirmed_todo fields retain their prior semantics.

#### Scenario: Valid Todo is exposed as a qualified source

- **WHEN** a linked canonical Todo has a matching issue and work_item in the same confirmed WorkAuthority and Tasks-v1 has at least one pending valid task
- **THEN** WorkAuthority.qualified_todos contains a record whose path and revision equal the source
- **THEN** the result includes issue, work_item, task counts, and qualification version

#### Scenario: Linked Todo with invalid metadata is not countable

- **WHEN** a linked Todo is missing or mismatches issue/work_item metadata, has an invalid Tasks section, or uses a noncanonical Todo path
- **THEN** Monitor may retain the ordinary source and rejection reason
- **THEN** WorkAuthority.qualified_todos excludes it even though its path link exists

#### Scenario: Source revision binds parser output

- **WHEN** Todo bytes change between scans
- **THEN** the source revision and qualification result are computed from the same bytes
- **THEN** no record pairs validation for one byte sequence with the revision for another

### Requirement: Generic path links target existing safe Monitor scanner sources

A kind:path link MUST identify an existing regular file under the repository root, without symlink traversal, and match a Monitor-supported workstream Todo, Superpowers spec, or Superpowers plan scanner path. The link check MUST happen before override mutation. It MUST NOT assert semantic Todo qualification, and it MUST preserve existing spec/design/plan linking use.

#### Scenario: Existing spec and plan links remain supported

- **WHEN** an operator links an existing safe spec, design, or plan path supported by the repository scanner
- **THEN** Monitor accepts the path link and correlation retains the source ownership

#### Scenario: Missing or unsafe path is rejected without mutation

- **WHEN** an operator links a missing, escaping, symlinked, non-regular, or unsupported scanner path
- **THEN** the link operation fails before writing the work-item override
- **THEN** the override file is byte-for-byte unchanged
