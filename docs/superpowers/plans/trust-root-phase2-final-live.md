---
status: accepted
work_item: trust-root-phase2-final-live
issue: 718
---

# Trust-root Phase 2 final normal-Codex live gate

## Scope

This is a bounded operator-evidence slice for the already-delivered
`trust-root-isolation` implementation. Read the existing
`docs/superpowers/evidence/trust-root-phase2-live-2026-08-22.md` and
`openspec/changes/trust-root-isolation/tasks.md` before changing anything.
Do not implement product code, change the installed runtime, alter systemd
units, touch credentials, or start a nested model invocation.

The purpose of this slice is to provide the missing normal-Codex start under
the final deployed hardening. The Cortex builder turn itself is that probe.

## Executor contract

The Manager must launch this slice with exactly:

- executor: `codex`
- model: `gpt-5.6-luna`
- reasoning effort: `max`
- persona: `builder`

The Manager's terminal record, not model-written prose, is authoritative for
the executor/model/reasoning argv, runtime mode, runtime surface, principal,
template instance, exit status, and completed provider usage. A provider
usage-limit response is a failed/unverified probe; do not turn it into a
success claim.

## Builder actions

1. Verify the checkout is clean at start and read the two existing evidence
   files named above.
2. Append a short section named `Final normal-Codex Cortex slice candidate` to
   the live evidence document. Include the current `PSC_SLICE_ID` (or the
   slice name from this plan), the declared executor/model/reasoning values,
   and the literal status `manager-terminal-verification-pending`.
3. State explicitly that this section is a candidate observation only: the
   Manager must independently bind it to a successful terminal job record
   before the final conjunctive task may be checked. Do not check the open
   task or write a successful normal-Codex claim from inside the builder.
4. Do not edit any other source, task checkbox, or installed/deployed file.
5. Run the bounded evidence-shape check below, then commit all intended
   changes with a Conventional Commit and leave the worktree clean.

## Bounded evidence-shape check

```bash
python3 - <<'PY'
from pathlib import Path

p = Path("docs/superpowers/evidence/trust-root-phase2-live-2026-08-22.md")
s = p.read_text(encoding="utf-8")
required = (
    "Final normal-Codex Cortex slice candidate",
    "manager-terminal-verification-pending",
    "codex/gpt-5.6-luna",
    "reasoning_effort: `max`",
)
missing = [item for item in required if item not in s]
if missing:
    raise SystemExit(f"missing evidence markers: {missing}")
PY
```

This check validates only that the candidate observation is shaped correctly;
it is not the terminal acceptance gate.

