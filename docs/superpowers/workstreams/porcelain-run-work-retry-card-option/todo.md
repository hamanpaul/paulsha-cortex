---
status: accepted
work_item: porcelain-run-work-retry-card-option
issue: 1030
domain_breadth: 0
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# `run work retry-card --card` Todo（#1030）

## Boundary and release decision

- Authority: live [#1030](https://github.com/hamanpaul/paulsha-cortex/issues/1030); exact scope is a missing porcelain selector.
- **本票非本輪 release blocker。** `v0.1.10` is already published. More importantly, the live #1020/#1021 recovery used the existing `--payload` card workaround and successfully queued Copilot jobs #962/#963. This demonstrates a usable bounded path; it does not mean either job or source issue is complete. #1030 can ship independently without changing their gates.
- #843 remains the broader recovery-action contract/conformance issue; it neither duplicates nor absorbs this concrete source bug. Before parallel execution, coordinate ownership of `tests/test_porcelain_run.py` if #843 is editing that file.
- Suggested sole work ID: `porcelain-run-work-retry-card-option`. This planning PR does not register a WorkAuthority, add a `.cortex/work-items.yaml` mapping, or start a Cortex run. Before implementation dispatch, verify canonical WorkAuthority uniqueness and issue binding, then read back any separately authorized registration.

## Sizing dimensions

- `domain_breadth=0`: one production module, `paulsha_cortex/porcelain/run.py`, in one CLI argument-to-request responsibility area.
- `state_consistency=0`: no new persistent field, writer, CAS, or manager state; reuse existing request object and exact-run/card contract.
- `acceptance_surfaces=2`: `fix-standard` has 2 gate-spine entries and this code change has all 3 applicable repo surfaces R-09/R-16/R-19.
- `orchestration=2`: current packaged `fix-standard` has 9 cards and 9 persona bindings.
- Official `current_sizing_snapshot()` result for this accepted spec/design/todo with `fix-standard`: `(4, yellow)` (`0+0+2+2`). The earlier draft scored `(6, yellow)` because its draft status added two spec-stability points. The accepted scope fits one bounded issue; no size split is indicated.

## Current source evidence

- Planning base: `origin/main` at `8b26d3702c398cb8dbda337bd596e26a0d96bc5b`; `VERSION=0.1.10`.
- Ran the umbrella CLI's `run work --help`: it lists `--expected-run-id`, `--builder-executor`, and `--builder-model`, but not `--card`. Replaying the live issue command exits 2 with `unrecognized arguments: --card worktree-isolation` before submission.
- `porcelain/run.py` does not declare or forward `card`; `control.contract` already requires the exact run ID and card, and `control.client.submit_request()` validates before atomic request write. The separate `recover work --card` entrypoint already exists and is out of scope.
- The `--payload` workaround accepted the two live recovery requests and queued jobs #962/#963; this is evidence that #1030 is not the active release blocker, not evidence of job completion.
- `_retry_card_action` has an existing extras allowlist. The CLI continues to merge supplied payload fields, but retry-card does not thereby accept arbitrary evidence refs; its existing Manager contract remains authoritative.

## Acceptance invariants

1. `retry-card --card <id>` arrives in the exact request args with `action`, `work_id`, `repo`, and exact expected run ID.
2. Existing paired `--builder-executor`/`--builder-model` is preserved exactly; no shared model registry mutation or manager policy change.
3. `--card` with another work action exits nonzero before writing/sending any request; it is never silently ignored.
4. Missing, malformed card or missing/malformed exact run ID continues to be rejected by existing control contract before request file creation.
5. Existing `--payload` card-only workaround remains accepted. CLI payload merge behavior remains intact, while retry-card still rejects fields outside its existing Manager allowlist; explicit `--card` cannot be shadowed by a conflicting payload card.
6. `cortex run work --help` marks `--card` as retry-card-only; README includes an actionable retry-card command and makes payload optional for card selection.
7. CLI parser/request regression tests assert both successful field mapping and fail-closed no-request cases; no claim about manager dispatch/job success from porcelain tests alone.

## Tasks

- [ ] **T0 freshness and ownership**: Before implementation, recheck #1030, #1020/#1021 and #843. Confirm the CLI contract remains `retry-card` + exact run CAS + card; use one owner for `tests/test_porcelain_run.py` if #843 work overlaps.
- [ ] **T1 tests / RED** (`tests/test_porcelain_run.py`): add the reported exact command shape with `--card worktree-isolation`, `--expected-run-id workflow-e45c1bd257ceec1c8285`, and paired builder override; assert request type/args exactly. Add non-retry action + `--card` and missing-card cases; assert nonzero exit and no request file. Add payload-only compatibility and conflicting explicit/payload selector test.
- [ ] **T2 source / GREEN** (`paulsha_cortex/porcelain/run.py`): add help text and forwarding. Reject card on other actions before submit; forward only for retry-card; preserve explicit selector against conflicting payload merge. Do not change shared control contract or Manager.
- [ ] **T3 docs / policy surfaces**: README run-work examples gain retry-card command with exact run id/card and optional builder pair; clarify that payload is optional for card selection and retry-card fields remain bounded by the existing Manager allowlist. Add implementation changelog fragment and `[Unreleased]` line; keep VERSION unchanged.
- [ ] **T4 validation**: run `tests/test_porcelain_run.py`, relevant CLI help smoke, required repository suite/checks, and PR-context `policy_check` including R-09/R-16/R-19. Keep request-mapping evidence separate from real dispatch evidence.
- [ ] **T5 delivery accounting**: review exact candidate and current CI/checks; distinguish code, tests, merge, installed CLI, and future recovery usage. Do not close #1020/#1021 or claim #962/#963 completed from this fix.

## Completion boundary

Complete #1030 when the explicit selector is discoverable and correctly forwarded only for retry-card, misuse and invalid input fail before enqueue, payload-only card compatibility remains deterministic without broadening the Manager allowlist, README/help/tests are updated, and repository delivery checks pass. Manager retry policy and downstream job outcomes remain governed by their existing evidence.
