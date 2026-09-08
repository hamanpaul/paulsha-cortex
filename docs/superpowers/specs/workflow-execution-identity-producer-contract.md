---
work_item: workflow-execution-identity-producer
status: pre-archive-contract
---

# Cortex workflow status execution identity producer contract

This is the producer-side handoff for `paulshaclaw#328`. The consumer may load
`tests/fixtures/workflow-execution-identity-828-status.json` as a direct Cortex
status payload. The fixture is de-identified and intentionally contains no live
machine paths, credentials, merge state, or dependency pin.

## Additive row fields

Every row representing a workflow execution in `in_flight`, `attention`, or
`recent_done` carries these fields without removing the section's existing
fields:

| Field | Meaning |
| --- | --- |
| `executor` | Executor recorded by the registry job, or the explicit planned executor; otherwise `null`. |
| `model` | Public projection of the registry job's `model_id`, or the explicit planned model; otherwise `null`. |
| `job_id` | Bound registry job ID for an actual execution; `null` when not dispatched or unknown. |
| `card` | The workflow card selected for the row. |
| `identity_source` | One of `in-flight`, `last-execution`, `planned`, or `unknown`. |
| `execution_state` | The registry job status for actual rows, or `not-dispatched` for planned/unknown rows. |

## Selection and consumer rules

- A current-card job in an in-flight state wins over a terminal job for the
  same run, repository, card, and phase.
- If no current-card job is in flight, the latest terminal job for that exact
  binding is `last-execution`; its state is not a claim that the job is still
  running.
- A `planned` row has no `job_id` and only exposes values explicitly present on
  the workflow step. A deterministic manager step is represented as
  `cortex-manager` / `deterministic`.
- An `unknown` row has `executor`, `model`, and `job_id` all set to `null`.
  Consumers must not infer identity from `phase`, `persona`, branch, worktree,
  or a job belonging to another run, repository, card, or phase.
- The `in_flight` row keeps its existing `state`/`slice_id` shape. A workflow
  `attention` row keeps `kind=workflow_run`, `run_id`, `work_id`,
  `current_phase`, and its existing blocking/action fields. A `recent_done`
  row keeps its existing manifest-derived fields. The identity fields are
  additive across these three sections.
- A consumer that reads an older status payload must use typed defaults for the
  additive fields and display identity as not provided; it must not manufacture
  a value.

The fixture is pre-archive producer evidence only. Downstream consumer changes,
installed producer/consumer integration, dependency pinning, issue closure,
merge, and archive remain separate operator-owned actions.
