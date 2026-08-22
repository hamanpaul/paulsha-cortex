# Trust-root Phase 2 live evidence — 2026-08-22

This is operator evidence for the deployed `trust-root-isolation` change. It is
not source-test evidence and contains no credential material.

## Delivered identity

- PR [#785](https://github.com/hamanpaul/paulsha-cortex/pull/785) is merged.
- `origin/main` is `13366c03b068012b499315ec39c69da131fa4577`.
- The deployed wheel is `paulsha_cortex-0.1.8-py3-none-any.whl`, SHA-256
  `99da849db2f885ac61609c5ffcac1f0c67f2c1c80bf74d15bd5a097f34b2a006`.
- Installed imports resolve from `/opt/cortex/venv/lib/python3.12/site-packages`;
  the seven changed product modules have the same SHA-256 as the exact `f14fb9e`
  candidate, and `spool_slot.exact_job_slot` is present.

## Static and service gates

- Candidate focused gate: `361 passed, 8 skipped, 4 subtests passed`.
- Full gate under restrictive `umask 0022`: `5045 passed, 41 skipped, 171
  subtests passed`.
- PR checks are green for persona scope, policy, Python 3.10–3.13, build, and
  smoke-install.
- `cortex-manager.service`, `cortex-monitor.service`, and
  `cortex-egress-proxy.service` are active with `ExecMainStatus=0`.
- Trust-root selfcheck reports `ok: true`, `job_writable_count: 0`; the
  derived-asset `unresolved` entries are the pre-existing warn-only diagnostic
  semantics. Registry equation reports no unregistered functions, dangling
  resolvers, or stale acknowledgements.

## Deployed own/foreign byte-identity probe

The Manager provisioned two instances per principal and launched the generated
units while the services were active:

| Principal | Unit | Instances | Surfaces | Result |
| --- | --- | ---: | ---: | --- |
| builder | `cortex-job@phase2-live-builder-a-20260822-fee9c1da.service` | A/B | 5 | own bytes exact; foreign bytes unchanged; all distinct |
| reviewer | `cortex-reviewer-job@phase2-live-reviewer-a-20260822-1b4549c7.service` | A/B | 4 | own bytes exact; foreign bytes unchanged; all distinct |

The root-side verifier returned `probe_pass: true`. Job logs contain only
`FOREIGN_WRITE_DENIED:<surface>` results and no `FOREIGN_WRITE_SUCCEEDED`.
The monitor was restored after the held probe. An earlier non-JSON probe file
was later quarantined by the normal monitor janitor; that cleanup is expected
and does not alter the captured pass result.

## Auth-refresh protocol

Both principals performed atomic temp+rename refresh publication with a
restrictive umask and named Manager read ACL. Manager reads succeeded and the
opposite principal reads were denied:

| Principal | Runtime auth path | Mode | SHA-256 |
| --- | --- | --- | --- |
| builder | `runtime/codex-home/builder/phase2-live-builder-a-20260822-fee9c1da/auth.json` | `0640` | `cd4f091a8a0e04be6a5b8e77dfc833ad539a1628650af760ffecfb9a12106ba6` |
| reviewer | `runtime/codex-home/reviewer/phase2-live-reviewer-a-20260822-1b4549c7/auth.json` | `0640` | `d4061cb1e406d137cbaac0f2e62306187f8825f9e209062f365572e3162ae444` |

The hashes match the expected refresh payloads; payload bytes are intentionally
not recorded here.

## Generated versus installed

The generated and installed hashes matched for the Manager, Monitor, egress,
builder, reviewer, gate, JIT variants, and job shim units. The generated and
installed polkit rule also matched (SHA-256
`c8fd053183d93df55ea0bc7934d09e28a8222a2ba80a60f02c7da087dae98f9a`), verified
as root because the policy file is root-readable only.

## Codex quota boundary and remaining task

Historical Cortex builder cards used `codex/gpt-5.6-luna` and completed with
recorded usage. A fresh normal-Codex recovery dispatch reached the provider but
was rejected by the provider usage limit (reported reset: 2026-08-27 11:36).
Per operator instruction, no retry was made and no successful normal-Codex turn
under this final deployment is claimed. Consequently the conjunctive live task
for “persistence, normal Codex start, auth-refresh, and generated-vs-installed”
remains unchecked until a later quota window; the other three live evidence
parts are recorded above.

The stale pre-merge Cortex workflow `workflow-16f4188674d36ab8809a` was retired
through the supported `retire-delivered` action after PR #783 was confirmed
merged. No workflow JSON or service state was edited by hand.

## Final normal-Codex Cortex slice candidate

- `PSC_SLICE_ID`: `phase2-final-live-codex-v6`
- Declared executor/model: `codex/gpt-5.6-luna`
- Declared reasoning effort: `reasoning_effort: `max``
- Status: `manager-terminal-verification-pending`

This is a candidate observation only. The Manager must independently bind it to
a successful terminal job record before the final conjunctive task may be
checked. This section does not check the open task or claim a successful
normal-Codex start from inside the builder.
