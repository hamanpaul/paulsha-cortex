---
status: proposed
work_item: stale-todo-authority-fail-closed
issue: 1065
---

# Claim WorkAuthority fresh-generation consumer

## Problem and outcome

Issue [#1065](https://github.com/hamanpaul/paulsha-cortex/issues/1065) covers a WorkAuthority reader that can return a matching last-good Todo row even when the latest Monitor refresh failed. A payload hash does not establish that the current link override was observed by a successful Monitor correlation.

This change defines a strict consumer contract: require the trusted latest-generation API from #1064 and the qualified canonical Todo source from #1063 before returning WorkAuthority. The result retains same-generation provenance so downstream work can inspect which generation, input revision, and source revisions justified the read.

## Requirements

1. A strict reader proves the latest attempt succeeded before it selects or returns a matching row.
2. That successful generation covers the current correlation-input revision and is within the trusted API's age limit.
3. A Todo in the generation is qualified by #1063 for canonical path, issue/work-item metadata, concrete Tasks, and safe existing path.
4. Generation, snapshot hash, correlation input revision, and source revisions in WorkAuthority come from that one generation.
5. Unknown, missing, legacy, malformed, expired, failed, or mixed-generation evidence fails closed even when a matching last-good row exists.
6. The explicit rate-limited last-known-good retirement behavior remains isolated from strict claim reads.
7. Generation-only advancement with unchanged semantic sources does not change the WorkAuthority/claim digest.

## Scope

- Consumer: `paulsha_cortex/coordinator/claim.py`.
- Tests: focused claim freshness cases and existing claim/retirement regression suites.
- Documentation: `docs/unified-work-lifecycle.md` and this issue's changelog entries.
- Dependency order: #1063 → #1064 → #1065 → #1054. #1054 owns Manager admission after this reader is ready. #1055 owns existing Candidate/PR recovery separately.

The unique work item is `stale-todo-authority-fail-closed`, with its canonical workstream Todo and this single active OpenSpec change. The planning PR does not register a runtime work item or start formal Cortex intake.
