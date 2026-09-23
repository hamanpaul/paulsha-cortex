---
status: accepted
work_item: terminal-replay-manifest-fence
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# `complete_tick` 權威 attempt 判定與 handoff manifest 覆寫 fence（#481）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#481`；[spec](../../specs/terminal-replay-manifest-fence-spec.md)、[design](../../specs/terminal-replay-manifest-fence-design.md)。前置：#862（`recovery-registry-receipt`，PR #954）merge 後才可派工；本票只讀它的 job `supersession`／`consumption` 欄位。
- 觸及模組（1 個 production 模組 → `domain_breadth: 0`）：`paulsha_cortex/coordinator/manager.py` 的 `complete_tick`，以及新增的 pure helper `_terminal_job_admission`、`_manifest_fences_job`、`_manifest_payload_equivalent`。`state_consistency: 1`：判定只讀既有 registry／manifest 持久資料，只抑制既有的單檔寫入，不新增 durable 欄位。
- 與 #497（`fix-superseded-terminal-replay`）分工：本票只做副作用前的權威 attempt 判定、manifest 覆寫 fence、manifest 內容冪等。durable supersession／consumption 寫入、recover 原子清 binding、CAS／request replay、完整 `run_tick` 派工驗收、restart 故障矩陣、evidence 位址都留在 #497。
- 不改 `registry.py`／`persona/handoff.py`／`verification.py`。不做 #496 dirty recheck、#487 分類、sentinel 收割／targeted complete、manifest 原子寫入、job／evidence GC。不新增 CLI，不改 `dispatch_gate_scan`／`_manifest_still_blocks_fanout`／`_existing_manifest_job_id` 的 None-set 語意。slice mutation 的跨 process CAS 不在本票。
- spec／design／本 todo 文字是 pinned authority，只准把 `[ ]` 翻成 `[x]`；澄清寫進 terminal reason。
- 留在 Manager 已 checkout 的分支上工作，不得自建 `wt/...` 分支。
- 不得 commit 或刪除 `docs/superpowers/plans/terminal-replay-manifest-fence.md`。
- tests 與 docs 不得寫死 `openspec/changes/<change>/` 路徑。

## 現場證據

- 2026-09-23 17:41:49（`runtime-pins/cortex-442fe23f`）：單次 tick 改寫 121 份 handoff manifest，恰好等於 `jobs.json` 中擁有兩個以上 terminal job 的 task 數；workflow lane 的 `wf-<hash>-<card>` 跨 retry 共用 task，同樣中招。
- #481（0.1.8 隔離 instance）：recover 回 `pending` 9 秒後被舊 failed job 撥回 failed、重綁舊 `builder_job_id`；單一 failed job 也會越過 recovery。
- 現行 main `7fa4716b`：
  - `manager.py:2468` 的唯一冪等判斷是單槽 manifest `job_id`；`:2695` 的 `write_manifest` 整份覆寫（`:2742` `completed_at: clock()`），抹掉 `superseded_*`。
  - `_slice_for_job` 對未綁定的 job 回 None，於是 `:2592-2604` 寫 `missing-slice-proof`；recover 的 `update_slice(builder_job_id=None)` 不清 binding（`registry.py:1650`）。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_terminal_replay_manifest_fence_481.py`，逐條對應 spec R7 (a)–(h) 與 design D8 矩陣。
      slice lane fixture 沿 `tests/test_coordinator_manager.py` 的 `_create_slice`（fake repo，spec／plan 為絕對路徑且 hash 相符）並設 `PSC_REPO_ROOT`，或沿 `tests/test_pre_candidate_recovery.py` 的 `_seed_repo`；workflow lane 沿 `tests/test_coordinator_manager.py` 的 `_make_workflow_job`／`FakeDispatcher`；每輪 clock 值不同。
      (a) 的 C 是 exited 並綁定、verification 回 status `needs_human`（記為 `verification-failed` action），A exited 未綁定，這樣修前才看得到 action 增長與 `missing-slice-proof` 寫入。(b) 另建全 failed、`candidate=None` 的 fixture，因為 candidate 是 SHA 時不允許 recover。(c) 的 None-set reason 用 `pinned-input-mismatch`，這是 failed job 能產生的 reason。
      修前必須因重播而 RED：(a)(b)(c)(d)(e)(f)(h)。首輪斷言 `errors == []`，不可靠 repo root 未設定提前 error 而假綠。
- [ ] **T2 source／權威 attempt admission（R1、D1–D3、D6）**：`complete_tick` 迴圈前取一次 `list_jobs()` snapshot，建立 `job_order` 與 `latest_terminal_order`；在 symlink 檢查之後、`_existing_manifest_job_id` 之前呼叫 `_terminal_job_admission`，命中即 `continue`，零副作用。
      依序判定：(a) disposition；(b) slice lane 綁定；(c) `operator-recover-pre-candidate`／`operator-abandon` 的 `at` ≥ job `created_at`；(d) 無 slice row 時同 task 已有較晚 terminal job；(e) manifest fence。
      skip reason 只寫 `logger.debug`。真正缺 slice 的最新 attempt 仍寫 `missing-slice-proof`。
- [ ] **T3 source／manifest 覆寫 fence（R2、D4）**：`_manifest_fences_job` 判 (i) superseded 且指向本 job、(ii) 指向較晚建立的 job。admission 用一次，`handoff.write_manifest` 前重讀 manifest 再用一次。
      寫入前命中 → 不寫檔、不進 `completed`／`completed_jobs`、不更新 `seen_slices`，並在 `warnings` 追加 `{"slice_id", "job_id", "warning": "manifest-write-fenced"}`。manifest 缺檔、壞檔或 `job_id` 未知不構成 fence。
- [ ] **T4 source／manifest 內容冪等（R3、D5）**：`_manifest_payload_equivalent` 以 JSON 正規化、去掉 `completed_at` 後比較，另需同 `job_id` 且既有 manifest 沒有 `superseded_at`。等價 → 不呼叫 `handoff.write_manifest`；`completed`／`completed_jobs`／`seen_slices`／`_discard_unpublished_evidence` 維持現行。
- [ ] **T5 tests／回歸與收斂（R4–R6）**：既有 tests 全綠、不改斷言，至少包含：`tests/test_coordinator_manager.py`（`CompleteTickReconcileTests`、`CompleteTickVerificationTests`（含 `test_manifest_hides_evidence_when_slice_update_fails`：輸入改變時 R6 重處理仍可改寫結果）、`CompleteTickWorkflowLaneGateTests`、#383 `run_tick` 案例）；`tests/test_pre_candidate_recovery.py`；`tests/test_coordinator_operator_actions.py`；`tests/test_dirty_recheck_idempotency_496.py`；`tests/test_pinned_spec_delivery_503.py`；`tests/test_repo_root_fail_closed_612.py`；`tests/test_outcome_taxonomy.py`；`tests/test_provider_failure_slice_lane.py`；`tests/test_executor_backoff_slice_lane.py`；`tests/test_coordinator_dispatch_discipline_e2e.py`；`tests/test_skill_ledger_manager_integration.py`。
      另補兩項斷言：「fresh `JobRegistry` 重建後連跑兩次 tick 結果不變」、「真正缺 slice 的單一 job 仍寫 `missing-slice-proof`」。最後跑全套 `python3 -m pytest -q`。
- [ ] **T6 documentation／changelog／CLI help**：新增 `changelog.d/terminal-replay-manifest-fence.md`，並同步 `CHANGELOG.md [Unreleased]`。
      `docs/unified-work-lifecycle.md` 在 #496 dirty recheck 段落旁補一段：`complete_tick` 只終局化權威 attempt，舊 attempt 為 audit-only；recover／abandon 之前建立的 job 被 fence；superseded 或較新的 manifest 不被舊 job 覆寫；內容相同不重寫 `completed_at`。
      本票不新增 CLI，以 help smoke 驗證 `python3 -m paulsha_cortex.cli complete --help` 輸出不變。
