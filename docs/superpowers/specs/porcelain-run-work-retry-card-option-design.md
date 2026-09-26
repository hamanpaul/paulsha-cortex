---
status: accepted
work_item: porcelain-run-work-retry-card-option
issue: 1030
---

# `run work retry-card --card` 設計（#1030）

## Decisions

### D1 — Extend only the existing porcelain argument builder

在 `paulsha_cortex/porcelain/run.py::_add_work_options()` 加 `--card`，原供 retry-card 使用；#557 後亦供 regenerate-gates 選擇 build job。由於 positional `action` 與 options 共用 parser，在 `_work_args()` 於 request 組裝前檢查：有 `args.card` 而 action 不是 `retry-card` 或 `regenerate-gates` 時 raise `ValueError`；兩個 action 都把明示的 `card` 加入 `provided_names`。`main()` 已在呼叫 `submit_request()` 前建立 args，會把此錯誤轉成 exit 2；不發送 request。

缺少或不合格式的 card 與 missing/stale `expected_run_id` 繼續由 `control.contract.validate_request()` fail closed。retry-card 仍要求 exact run ID 與 regex 限定的 card ID；#557 另讓 contract 驗證 regenerate-gates 選填 card 的相同格式，不改 retry-card 的 action admission。

### D2 — Prevent explicit option shadowing while keeping the workaround

`_work_args()` 現在先建 CLI payload，再以 `--payload` object 更新，payload 可以覆蓋一般 args。保留 payload-only `card`，因其已用於 #1020/#1021；當使用者也傳 `--card` 時，先比對 extra `card`：不同值即拒絕，避免 selector 靜默改變。相同值作冗餘相容輸入，由測試固定；其他 payload 欄位仍照現有 CLI merge 行為傳送。這不擴大 Manager 接受的欄位：retry-card 的 `_retry_card_action` 只接受既有 allowlist，任意 evidence refs 仍會被拒絕；各 action 的 payload admission 由原 Manager contract 決定。Protected top-level fields (`action`／`repo`／`work_id`) 維持現況。

### D3 — Preserve run-scoped override and Manager semantics

`builder_executor`／`builder_model` 已由 `_work_args()` 轉送，僅增補 `card`，不重排、不清除、不改驗證。request 仍是 `work-action`；expected run CAS、card grammar、正式 `retry-card` 準入和 force-new-card dispatch 全由既有路徑處理。此 patch 的 CLI enqueue 測試不宣稱真的派出或完成 job。

### D4 — Keep issue and recovery-matrix ownership distinct

#1020/#1021 已用 `--payload` workaround 正式建立 jobs #962/#963；#1030 不需等待其結果才能實作，也不會改動那些 run。#843 維持 broad recovery contract conformance owner；如其工作同時編輯 `tests/test_porcelain_run.py`，先由單一 owner 整合測試檔，避免平行覆寫。

## File and work-item mapping

| Candidate work item | Production/test/docs ownership | Purpose |
| --- | --- | --- |
| `porcelain-run-work-retry-card-option` | `paulsha_cortex/porcelain/run.py`; `tests/test_porcelain_run.py`; `README.md`; required changelog surfaces | Single bounded issue owner; no separate child package needed. |

`git grep` against current `origin/main` and `.cortex/work-items.yaml` found no candidate-ID entry before these planning files were added. This PR leaves the candidate unregistered; before implementation dispatch, verify canonical WorkAuthority uniqueness and issue binding, then read back any separately authorized registration.

## Source evidence at planning base

- Planning base is current `origin/main`, `8b26d3702c398cb8dbda337bd596e26a0d96bc5b`; `VERSION=0.1.10`.
- Executed the real umbrella CLI entrypoint with `python3 -c 'from paulsha_cortex.cli import main; main(["run", "work", "--help"])'`: `--card` is absent, while `--expected-run-id`, `--builder-executor`, and `--builder-model` are present. Replayed the exact #1030 argument shape through the same entrypoint; argparse exits 2 with `unrecognized arguments: --card worktree-isolation`.
- `paulsha_cortex/porcelain/run.py:52–81` defines the shared `run work` options without `--card`; `:132–170` builds forwarded fields without `card`, then merges optional `--payload` JSON after CLI fields.
- `paulsha_cortex/control/contract.py:164–186` already enforces exact `expected_run_id` and a syntactically valid `card` for `retry-card`. `paulsha_cortex/control/client.py:37–40` validates the request before its atomic file write, so missing or malformed contract fields do not create a request file.
- `paulsha_cortex/porcelain/recover.py:60–73,103–114` already exposes and forwards `--card` for `recover work`; that separate entrypoint is context only and stays outside this change.
- `tests/test_porcelain_run.py:129–162` verifies CLI-to-request mapping. Extend this public parser/request path rather than testing a private argument helper alone.
- `README.md:328–342` documents `run work` commands and run-scoped builder overrides, but has no `retry-card` command. The README and `cortex run work --help` are the user-facing documentation surfaces in this issue.
- Live #843 defines broad recovery-action matrix/conformance ownership and says concrete producer bugs stay with their original issue. #1030 is the specific `run work` producer gap; do not expand this plan to the recovery matrix or the separate `recover work` entrypoint.

## Risks

- A parser flag accepted for every work action could become an ignored typo. The action check must occur before request submission.
- Existing payload merge order can shadow explicit values. Test mismatched `--card` plus payload card and keep payload-only workaround intact.
- CLI request tests prove only a valid request was written. They do not prove Manager admission, identity resolution, or job completion; existing `retry-card` tests remain the authority for those layers.
