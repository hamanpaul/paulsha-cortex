# Preserved PR-context preflight metadata

This record preserves an existing successful preflight observation from
`wf-68ec7e3df5-subagent-build-219`. It is not a claim that this card executed
that run, and it is not a producer signature or an independent time
attestation.

## Source event

- **Producer job:** `wf-68ec7e3df5-subagent-build-219`.
- **Source event selector:** exact nested `item.id=item_41` with outer
  `type=item.completed` and nested `item.type=command_execution`; no recursive
  log-text search was used to assemble the output.
- **Tested code subject:**
  `81b854e4eea25162d50cab9ce4d0b0a8c4cbbacc`.
- **New preservation job:** `wf-68ec7e3df5-subagent-build-228`.
- **Preservation observed UTC:** `2026-09-09T02:38:44Z`.
- **Source workflow JSONL:** manager-retained workflow log; SHA256
  `b291dceaf5e974cffdb691a553cf66f60e3ed15563478b8ce634e2da6bf77fb9`,
  3732813 bytes at preservation. The source log path is intentionally not
  published.

## Path-redacted command record

The following is a path-redacted rendering of the command recorded in
`item_41`; it is not the byte-preserved command text. Private absolute
checkout prefixes are represented by role aliases.

```text
engine_root=$(mktemp -d /tmp/paulsha-conventions-219.XXXXXX)
git -C <CONVENTIONS_CHECKOUT> worktree add --detach "$engine_root" 9e7fabbf0b5eea9ad933fa6798764b723934a0b7
status=0
"$engine_root/skills/preflight-ci/scripts/preflight.sh" --offline --pr-title 'fix(agy-probe): 補齊建構失敗 containment' --pr-body-file /tmp/agy-probe-pr-body.md --pr-labels '' --base main --head feature/851-agy-probe-construction-containment || status=$?
git -C <CONVENTIONS_CHECKOUT> worktree remove --force "$engine_root"
exit "$status"
```

The source command did not pass `--repo-visibility`; the preserved output
therefore records `visibility=unknown`. That observation must not be combined
with a separately executed public-visibility preflight as though they were one
run.

## PR context established by the source events

- **Title:** `fix(agy-probe): 補齊建構失敗 containment`
- **Labels:** empty
- **Base:** `main`
- **Head:** `feature/851-agy-probe-construction-containment`
- **Visibility:** `unknown`
- **Body source:** the body file read in the source workflow's `item_39`
  observation; it was 626 bytes with SHA256
  `ef19afd332af2c288ba414626afdf19e4bdcafe32f58f9c711e0e5049bab1150`; the
  text was:

```text
Closes #851

## 摘要

- 補回 `agy-probe-construction-containment` 自有的 active OpenSpec task ledger。
- 僅記錄既有 candidate 已完成的 AGY probe containment、regression、docs 與 changelog pre-archive work。
- 保留 archive、merge、issue closure、installation/loaded-runtime 與 #824 dependency 為 Manager/operator 後續責任。

## 驗證

- [x] focused AGY regression suite 通過。
- [x] authoritative full pytest suite 通過。
- [x] OpenSpec specs validation、diff check 與 checkout 外 CLI help smoke 通過。
- [x] PR body 與 issue 使用 closing keyword，未使用 exemption label。
```

## Result and byte binding

- **Pinned engine:** `hamanpaul/paulsha-conventions@9e7fabbf0b5eea9ad933fa6798764b723934a0b7`.
- **Exit code:** `0`.
- **Preserved aggregated output:**
  `preflight-219-transcript.txt`, SHA256
  `4d92e207c7a740a3b01e17945634474cdbabfe92e05f31da3440c499c8005868`,
  383 bytes.
- The transcript is the selected event's complete merged stdout/stderr value,
  copied without cleaning, truncation, or rerun. Its hash binds retained bytes,
  not producer identity or execution time.
