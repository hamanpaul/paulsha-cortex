---
status: accepted
work_item: monitor-canonical-todo-qualification
issue: 1063
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

# Monitor canonical Todo qualification Todo（#1063）

## Boundary

- Owner issue: [#1063](https://github.com/hamanpaul/paulsha-cortex/issues/1063); parent/consumer: [#1054](https://github.com/hamanpaul/paulsha-cortex/issues/1054); related parent: [#1051](https://github.com/hamanpaul/paulsha-cortex/issues/1051).
- Unique work item: monitor-canonical-todo-qualification. This spec, design, Todo, own OpenSpec change and .cortex/work-items.yaml row use the same work_item.
- Canonical Todo path for this work item: docs/superpowers/workstreams/monitor-canonical-todo-qualification/todo.md.
- #1054 owns the first-Builder Manager gate. #1064/#1065 own Monitor generation and WorkAuthority freshness. #1055 owns existing Candidate/PR recovery.
- A path link is generic source ownership: it requires an existing safe Monitor scanner source but does not assert Todo qualification. Existing spec/design/plan path links remain supported.
- Unique mapped binding: issue #1063 + OpenSpec monitor-canonical-todo-qualification + this canonical Todo path. Superpowers spec/design artifacts carry the same work_item and remain distinct from Todo qualification.
- No product code, tests, formal run, Monitor snapshot, registry, journal or service state is changed by this planning packet; the packet itself is delivered as a Draft PR.

## Acceptance

- [ ] One deterministic canonical Todo qualification contract validates exact path, positive issue frontmatter, exact work_item, same WorkAuthority issue membership, explicit path ownership and Tasks-v1.
- [ ] A typed qualification result binds source_id/path/revision/issue/work_item/Tasks result. WorkAuthority exposes only passed results as qualified_todos; raw mapped_todo_paths and confirmed_todo semantics stay unchanged.
- [ ] Generic kind:path link validation requires a current regular non-symlink file under repo root from Monitor-supported Todo/spec/plan scanner roots and runs before override write. It does not parse Todo metadata.
- [ ] Any rejected path link performs zero override mutation; stale invalid links can still be removed by exact unlink.
- [ ] Invalid but linked Todos remain uncounted; spec/design/plan path-link use remains intact.

## Tasks

- [ ] **T0 source and owner verification**: re-read live #1063/#1054/#1051 and baseline at RepoWorkProvider._scan_sources(), _frontmatter_work_item_text(), correlation._validate_repo_path(), work_actions._mutate_override(), and WorkAuthority loading. Keep their authority boundaries separate.
- [ ] **T1 source/tests Todo parser**: add the canonical Todo path predicate and TasksValidation/TodoQualification-v1 records; parse positive issue, work_item, exact Tasks heading, unique task IDs, title/action fields and pending count from the same safe bytes used for source revision.
- [ ] **T2 source/correlation qualification**: join candidate issue to a confirmed same-repo GitHub issue in the same work item; require exact work_item and explicit path ownership; retain typed rejection reason; never qualify spec/design/plan or infer GitHub author identity.
- [ ] **T3 source/WorkAuthority output**: expose strict WorkAuthority.qualified_todos records whose path and revision match the source and whose issue/work_item/path ownership checks pass; preserve mapped_todo_paths, confirmed_todo, ship and existing reader behavior.
- [ ] **T4 source/path-link guard**: share a canonical scanner-source validator across correlation and work link. Before atomic override write require repo-relative supported scanner path, strict existing regular file, repo containment and no symlink traversal; keep existing spec/design/plan links. Rejection must leave bytes unchanged; exact unlink remains available for stale bad links.
- [ ] **T5 tests/source boundary fixtures**: cover valid Todo, issue mismatch, work_item mismatch, missing/empty/placeholder/duplicate/checked-only/malformed Tasks, noncanonical Todo, ordinary existing non-scanner file, missing path, repo escape, symlink traversal, and successful spec/design/plan link. Assert rejected writes do not mutate overrides and invalid linked Todos do not enter qualified_todos.
- [ ] **T6 documentation and CLI**: update README, docs/unified-work-lifecycle.md and existing work-link CLI help to distinguish generic path eligibility from semantic Todo qualification; explain owner-published as repository correlation, not GitHub author proof.
- [ ] **T7 OpenSpec/changelog/policy**: update the own OpenSpec proposal/design/tasks/spec delta, add implementation branch changelog fragment and Unreleased entry, keep VERSION unchanged, and run strict own-change validation plus canonical specs validation, PR-context policy, and diff check.
- [ ] **T8 required implementation verification**: run focused parser/correlation/link tests and repository-required tests. Do not test or alter Manager dispatch/freshness/recovery in this issue; those belong to #1054/#1064/#1065/#1055.

## Five-dimension official sizing

The repository helper is run on the accepted six-view packet: Superpowers spec/design/Todo plus this change's OpenSpec proposal/design/tasks. Combo is fix-standard.

| Dimension | Score | Basis |
|---|---:|---|
| domain_breadth | 1 | One connected source-authority contract across Monitor parsing/correlation, WorkAuthority projection and work-link mutation; no Manager lifecycle gate. |
| state_consistency | 1 | Derived typed result is revision-bound in the existing snapshot; no new mutable store, claim writer, generation or workflow transition. |
| acceptance_surfaces | 2 | Official fix-standard gate spine and repository R-09/R-16/R-19 surfaces. |
| spec_stability | 0 | All six planning views are accepted, complete and carry no blocking marker. |
| orchestration | 2 | Official fix-standard card/persona topology. |
| **Total / band** | **6 / Yellow** | Official current_sizing_snapshot result for the accepted packet, not implementation readiness. |

Recompute sizing at formal intake if any Manager gate, generation/freshness writer, claim/delivery state or additional production responsibility enters scope. #1054 remains blocked until #1063 implementation provides the qualified Todo and generic safe path-link contracts.

## Delivery status

This planning packet does not implement the issue acceptance criteria, invoke Cortex intake, operate a product run/snapshot, change VERSION, merge, or deploy. It is delivered by a Draft PR; the implementation tasks above remain unchecked.
