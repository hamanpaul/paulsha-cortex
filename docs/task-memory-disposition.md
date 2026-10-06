---
status: active
work_item: task-memory-disposition
---

# Task-memory disposition receipts

When a workflow attempt receives task-memory content, its terminal result should include
`task_memory_disposition`: one entry for every note id delivered to that attempt. Each entry
has a verdict (`applied`, `consulted_no_change`, `not_relevant`, `stale_or_wrong`,
`already_known`, or `not_read`) and a non-empty reason of at most 140 characters. Attempts
that received no note do not need the field.

The Manager removes this optional field before terminal shape and lifecycle checks. A missing,
malformed, or incomplete report creates an `unreported` receipt with reason `missing`,
`malformed`, or `note-set-mismatch`; it never changes the card result. A valid report creates
one `disposition-reported` receipt per note. Receipts keep each verdict and the SHA-256 of the
reason, but never store the reason text.

## Relationship to `task_memory_applied`

`task_memory_disposition` is authoritative when it validates. A coexisting legacy
`task_memory_applied` entry is harvested only when the disposition for that note is `applied`;
for other verdicts, the Manager ignores the legacy evidence entry. The legacy field remains
supported when a terminal has no valid disposition field, so existing executors can continue
to report file evidence during migration. A disposition of `applied` records the verdict; a
separate valid `task_memory_applied` entry is still needed to create an applied evidence receipt.

Build evidence can cite a repo-relative Candidate file and its committed SHA-256. Verification
and review evidence can instead use `evidence_ref: "finding:<key>"` for a finding present in
that accepted terminal's diagnostics or findings. For that reference, `evidence_sha256` is the
SHA-256 of the literal UTF-8 string `finding:<key>`; the Manager resolves the key and stores
the SHA-256 of the canonical accepted finding. Finding references are not accepted on build
cards or when the key is absent.

## Delivery evidence

Each `context-delivered` receipt includes `delivery_sha256`, the SHA-256 of the exact
host-authored inline memory block sent in the prompt (its heading, untrusted-context warning,
and delivered note lines). The block text is not copied into the receipt. This proves which
block the host constructed and sent; it does not prove that an executor read or understood it.

## Receipt metrics

The append-only JSONL receipts live under
`$PSC_COORDINATOR_ROOT/task-memory/receipts/`. With `jq` installed, run these queries from a
shell with `PSC_COORDINATOR_ROOT` set to the active coordinator root.

Fill rate counts validly reported notes divided by notes returned or delivered:

```sh
jq -s '
  def note: [.repo, .work_id, .workflow_run_id, .attempt_id, .note_id] | @json;
  ([.[] | select(.event == "context-delivered" or .event == "content-returned") | note] | unique) as $delivered
  | ([.[] | select(.event == "disposition-reported") | note] | unique) as $reported
  | {delivered_notes: ($delivered | length), reported_notes: ($reported | length),
     fill_rate: (if ($delivered | length) == 0 then null else ($reported | length) / ($delivered | length) end)}
' "$PSC_COORDINATOR_ROOT"/task-memory/receipts/*.jsonl
```

Verdict counts:

```sh
jq -s '[.[] | select(.event == "disposition-reported") | .verdict]
  | sort | group_by(.) | map({verdict: .[0], count: length})' \
  "$PSC_COORDINATOR_ROOT"/task-memory/receipts/*.jsonl
```

Unreported reasons:

```sh
jq -s '[.[] | select(.event == "unreported") | .reason]
  | sort | group_by(.) | map({reason: .[0], count: length})' \
  "$PSC_COORDINATOR_ROOT"/task-memory/receipts/*.jsonl
```
