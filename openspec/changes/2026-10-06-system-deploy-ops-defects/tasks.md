---
status: accepted
work_item: system-deploy-ops-defects
---

# Tasks

- [x] [GREEN] T1: Keep `github-terminal` usable with a read-only Monitor checkout; Manager-owned fetch/fast-forward must not write `FETCH_HEAD`; cover read-only behavior in regression tests.
- [x] [GREEN] T2: Share the 900-second claim freshness limit with system and user Monitor refresh validation; reject inconsistent intervals and cover both configurations.
- [x] [GREEN] T3: Update the packaged model roster and provide a validated Manager-owned overlay command; retain the packaged identity regression test.
- [x] [GREEN] T4: Supply system deployment quota shadow admission when no operator quota file exists; cover identity bindings and non-enforcement.
- [x] [GREEN] T5: Resolve system Monitor workspace from `PSC_REPO_ROOT`; cover project config and generated unit settings.
- [x] [GREEN] T6: Synchronize the Manager source checkout to GitHub's default branch safely; cover a newly visible work item and avoid `FETCH_HEAD` writes.
- [x] [GREEN] T7: Reversibly park pre-existing automatic slice specs once; cover migration marker behavior and later specs.
- [x] [GREEN] T8: Document behavior and operator checks in the Trust Root runbook; update the changelog fragment and `[Unreleased]` entry.
