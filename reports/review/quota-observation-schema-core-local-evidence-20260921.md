---
receipt_id: quota-observation-schema-core-local-evidence-20260921
work_item: quota-observation-schema-core
status: local-complete
source_candidate_base: e78e482fbde04beb7a7639dc39da6fe87da9ebba
---

# Quota observation schema core — local evidence receipt

本 receipt 對應 #866 的 pre-archive 本地證據。它只陳述目前 worktree 的本地驗證、檔案
scope/hash 與仍待下游核收的項目；不冒簽 Manager independent review、remote CI、
archive、PR/merge、installed/live 或 #836 的 B/C/D integration 已完成。

## Scope / hashes

| Path | SHA-256 |
| --- | --- |
| `paulsha_cortex/coordinator/quota_observation.py` | `3d79cffde8ef969dc7df6490e56bc13e40de799aacc077543b1898891e8d9f50` |
| `tests/test_quota_observation.py` | `38d69bf1645cf5162870baf9bf953dd27f1e50ada227a311cb628231a69c0aca` |
| `README.md` | `031bc8a13cb2a3d1717ccca7d05ea932b782e954df8b35043d39249b47250ddc` |
| `docs/unified-work-lifecycle.md` | `0080693ddb4cfbec1dee9c2561f8df43f97a06a594560ffdd4ef1d806c4e9ab4` |
| `CHANGELOG.md` | `d22a3ccaae7780315c858a487701e99bdba5663ae39ee2083cf8c1f7daacc32d` |
| `changelog.d/quota-observation-schema-core.md` | `5d7b42ffc0e546bcf6b2b13273c68feff82bf6be546955873e41c7dc43fd6937` |
| `docs/superpowers/workstreams/quota-observation-schema-core/todo.md` | `e9c20fef161e925d0414ed652365750d22d70f0f86123fd18df97fb8c10401bd` |
| `openspec/changes/quota-observation-schema-core/tasks.md` | `f4dba7a664f4f147f68c291d2ff9e254adfdf2e9b8920e2945e2f9db02c1d0cf` |

`openspec/changes/quota-observation-schema-core/tasks.md` 僅回復到 `1ec2d732` 的未勾選
baseline；active workstream 進度只更新 `docs/superpowers/workstreams/.../todo.md`
的 checkbox 字元。

## Command ledger

| Command | Exit | Observed result |
| --- | ---: | --- |
| `python3 -m pytest tests/test_quota_observation.py --collect-only -q` | 0 | `82 tests collected` |
| `python3 -m pytest tests/test_quota_observation.py -q` | 0 | `82 passed` |
| `python3 -m pytest tests/ -q` | 0 | `6191 passed, 41 skipped, 186 subtests passed` |
| `env -u PSC_REPO_ROOT "$HOME/prj_pri/paulshaclaw/.venv/bin/python" -m pytest -q` | 0 | `6188 passed, 44 skipped, 186 subtests passed` |
| `HOME=$(mktemp -d) python3 -m pytest -q` | 0 | `6188 passed, 44 skipped, 186 subtests passed` |
| `openspec validate quota-observation-schema-core --strict --no-interactive` | 0 | `Change 'quota-observation-schema-core' is valid` |
| `openspec validate --specs` | 0 | `27 passed, 0 failed` |
| `python3 -m paulsha_cortex.cli --help` | 0 | printed CLI help |
| `python3 -m build --wheel --outdir <tmp>` | 0 | wheel built successfully |
| `<tmp-venv>/bin/pip install <wheel>` | 0 | isolated wheel install smoke passed |
| `<tmp-venv>/bin/python -m paulsha_cortex.cli --help` | 0 | installed wheel prints CLI help |
| `python3 -m policy_check.preflight --repo . --offline --pr-title 'feat: 完成 quota observation schema core pure-data contract' --pr-body-file <tmp> --pr-labels '' --base main` | 0 | `context: base=main head=feature/866-quota-observation-schema-core labels=0`, `engine PASS @ 9e7fabbf0b5eea9ad933fa6798764b723934a0b7`, `policy PASS`, `openspec PASS`, `tests PASS 207.89s`, `PREFLIGHT PASS` |
| `git diff --check` | 0 | no whitespace / conflict marker errors |

## Coverage ledger

| Item | Status | Local evidence |
| --- | --- | --- |
| I1 pure / no consumer wiring | verified | no-I/O sentinel test, README + lifecycle boundary docs, scoped source-trace test for `registry.update_headless_result() -> extract_usage()` / `StreamEvidence` |
| I2 strict shape / limits / immutability | verified | parser-sealed DTO tests, strict shape/schema tests, cycle/depth/node/string/root-byte seam tests, duplicate/cap tests, deep mutation snapshot tests |
| I3 frozen profile framing | verified | request/resolved/observed/actual domain roundtrip tests; fake well-shaped `actual` key stays opaque |
| I4 explicit caller binding | verified | profile/group subject tests, duplicate/missing known constraint tests, duplicate descriptor PoolRef rejection |
| I5 window identity / consistency | verified | descriptor window matrix tests, descriptor-backed known scope tests, interval/instant/unknown instance checks |
| I6 quantity / unit semantics | verified | standalone+inline unit union tests, conflict tests, unknown-unit acceptance/rejection matrix, exact+bounds amount tests, float/bool/exponent/NaN/Infinity negative vectors |
| I7 source / coverage truth separation | verified | source.method matrix tests, limit-signal gating, coverage shape tests, unavailable event-ID path |
| I8 freshness only classifies time | verified | TTL/reset/window-end equality, missing inputs, future source time, allowed skew tests |
| I9 source-event identity vs receipt | verified | available/unavailable event identity tests, receipt/adapter changes keep the same key, and same source event intentionally collides across different measurements |
| I10 no admission / authorization claim | verified | no dispatch/admission fields added; docs explicitly preserve schema-only boundary and unchanged existing usage path |
| AC1 | verified | public API + strict input/error + immutability tests |
| AC2 | verified | synthetic same-account short/week constraints, different-account scope resolution, group subject, known/unknown binding and window coverage tests |
| AC3 | verified | D4a C1/C2/C3-style quantity/unit/bounds/conflict tests |
| AC4 | verified | explicit-now freshness + event-identity stability tests |
| AC5 | verified | docs/tests keep opaque profile/source truth; no qualification/authorization helper added |
| AC6 | partial / local-only | local pytest, OpenSpec, policy preflight, CLI help, wheel smoke all passed; remote Python matrix, PR-context on GitHub, and independent review remain pending |

合法 v1 payload 在 per-string / cardinality caps 下就會先撞到更窄的 grammar 上限，因此
standalone UnitDefinition 的 `2048` byte ceiling 與跨輸入 `1146880` semantic-byte
ceiling 在這版主要以 fail-closed seam（overflow-before-shape）與 exact cardinality
(`unit_catalog=16` / `17`) 方式覆蓋；沒有把 unreachable byte count 冒稱成可達的 happy path。

## Downstream pending

- Manager independent exact-candidate review and finding disposition
- GitHub PR creation, hosted PR-context checks, review threads, merge
- Remote Python 3.10–3.13 CI / build / install smoke
- own OpenSpec archive, post-archive reverify, installed revision and live maintenance evidence
- #836 downstream B/C/D work items (source adapters, durable ledger/reconcile, shadow/admission wiring)
