---
status: accepted
work_item: retry-build-preserve-proof
---

# retry-build 在 launch 前失敗時保留 slice 既有 proof 設計

## Decisions

### D1 快照：只對恢復態 slice、在任何寫入前取

`dispatch_ready` 每個就緒單位迭代的第一步（`pin_dispatch_inputs` 之前）呼叫新 helper `_recovery_slice_snapshot(dispatcher, slice_id) -> dict | None`：取 `registry.get_slice(slice_id)` 的 deep copy（`copy.deepcopy` 或 JSON round-trip，不得與 live row 共用 nested dict）。registry 為 None、沒有 `get_slice`、拋 `KeyError` 或任何例外、回傳非 dict、`state ∉ {"needs_human", "failed"}` 一律回 None；helper 本身永不拋錯。回 None 的單位走現行程式碼路徑、行為逐字不變（spec R5）。快照只存在記憶體，不持久化。

### D2 `launched` 旗標界定「launch 前」

per-slice 區域變數新增 `launched = False`，在 `active_launcher.launch(...)` 回傳之後、`_attach_launch_handle` 之前設為 `True`。另新增 `written_dispatch_base = None`，供 D4 前置條件比對：`_record_pending_slice` 生效後設為 `early_dispatch_head`；`_mark_slice_building` 成功且傳入的 `base_sha or dispatch_head` 非 None 時改為該值（對齊 `update_slice` 把 None 當未提供的語意）。except 分支只在 `pinned_inputs is not None and snapshot is not None and not launched` 時走恢復態處理（D3–D5；`pinned_inputs is None` 與現行一樣不寫 slice）；`launched` 為 True（handle 已拿到、attach 才失敗）維持現行 `_fail_launching_job`＋`_mark_slice_needs_human`（spec R5 e）。

### D3 except 分支不替恢復態 row 補 repin

現行 `if pinned_inputs is not None and not slice_recorded: _record_pending_slice(...)` 加上 `snapshot is None` 條件：首次派工與非恢復態 row 仍照舊補建／repin（spec R5 a／b），恢復態 row 不補。`if job is not None: _fail_launching_job(...)` 不變——新 job row 照舊標 `failed`、`runtime_diagnostic.reason = "launch-failed"`，保留稽核。

### D4 還原：組合既有 registry writer，不新增 writer

新 helper `_restore_recovery_slice(dispatcher, slice_id, *, prior, pinned_inputs, written_dispatch_base, launch_job_id) -> bool`，只在 `slice_recorded`（本次 repin 已生效）時呼叫，依序：

1. 重讀 `current = registry.get_slice(slice_id)`；spec R4 前置條件逐項比對，任一不成立 → 回 False，不寫任何東西：`spec.hash`／`plan.hash`／`verification.hash`／`target_branch`／`target_remote` 等於 `pinned_inputs` 對應值；`dispatch_base == written_dispatch_base`；`builder_job_id in (None, launch_job_id)`；`reviewer_job_id is None`、`candidate is None`、`current_evidence_refs == []`、`current_evaluation_refs == []`、`current_verification_evidence_hash is None`、`gate_state == "pending"`；`state == prior["state"]`，或 `state == "building"` 且 `builder_job_id == launch_job_id`；`len(actions)`／`len(evidence_history)`／`len(evaluation_history)` 等於快照。涵蓋步驟 3–4 會覆寫的每個欄位，加上三條 history 長度（其他 writer 經 `record_action` 寫入必改長度，`registry.py:1723-1748`），不覆寫他人已寫入的變更。前置檢查與寫入之間仍非原子（見下方代價段）。
2. `current["state"]` 不在 `{"pending", "needs_human", "failed"}`（實務上只有 `_mark_slice_building` 寫入的 `building`）→ `registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")`（building→needs_human、gate pending→needs_human 皆為既有合法轉移），讓下一步 `repin_slice` 可受理。
3. `registry.repin_slice(slice_id, spec_path=prior["spec"]["path"], spec_hash=prior["spec"]["hash"], plan_path=prior["plan"]["path"], plan_hash=prior["plan"]["hash"], target_branch=prior["target_branch"], target_remote=prior["target_remote"], verification_hash=prior["verification"]["hash"], verification=prior["verification"].get("contract"), dispatch_base=prior.get("dispatch_base"))`：pin 欄位與 `dispatch_base` 回到 prior，綁定／candidate／current refs／evidence hash 被清成 None／`[]`、`gate_state` 成 `pending`。
4. `registry.update_slice(slice_id, state=prior["state"], gate_state=prior["gate_state"], builder_job_id=prior.get("builder_job_id"), reviewer_job_id=prior.get("reviewer_job_id"), candidate=prior.get("candidate"), current_evidence_refs=list(prior.get("current_evidence_refs") or []), current_evaluation_refs=list(prior.get("current_evaluation_refs") or []), current_verification_evidence_hash=prior.get("current_verification_evidence_hash"))`：`update_slice` 把 None 當「未提供」，而第 3 步已把這些欄位清成 None，所以 prior 為 None 的欄位自然維持 None，非 None 者寫回；state／gate 轉移（needs_human→needs_human／failed、pending→needs_human／failed／pending）皆由既有 `SLICE_STATE_TRANSITIONS`／`GATE_STATE_TRANSITIONS` 驗證。
5. 回 True。

不新增 registry 方法的理由：#862（`recovery-registry-receipt`）與 #497（`fix-superseded-terminal-replay`）正在 `registry.py` 建 revision／receipt／supersession 原語並要求「所有 effective writer 計次」，新增 writer 會擴大其範圍並製造並行 PR 衝突。代價是還原為 2–3 次 persist 而非單次原子寫：任一步拋錯或中途崩潰時，row 停在「已 repin、proof 已清」，也就是現行結果，不會比修正前更差（spec R4 退路、Boundary 不宣稱原子）。

### D5 收尾：記一筆 `dispatch-failed`，不動 state

恢復態失敗交給新 helper `_settle_recovery_dispatch_failure(dispatcher, slice_id, *, prior, repinned, pinned_spec_hash, launch_job_id, exc) -> None`：

- `repinned` 為 True → 呼叫 D4；D4 回 False 或拋錯 → `logger.warning` 一行（slice_id、原因）後改走現行 `_mark_slice_needs_human(dispatcher, slice_id, reason=str(exc))`，結束。
- 已還原或本次未 repin → `registry.record_action(slice_id, action="dispatch-failed", actor="manager")`（不帶 `state`／`gate_state`，action entry 自動記錄保留後的值），並 `logger.info` 一行（slice_id、是否還原、例外摘要）；`record_action` 若拋錯只記 log、不再擴大寫入，也不改走 `_mark_slice_needs_human`（它同樣要呼叫 `record_action`，且會把 `failed` 改寫成 `needs_human`）。這與現行 `_mark_slice_needs_human` 吞掉 `record_action` 失敗（`autonomy.py:1145-1154`）一致，spec R2 已把這筆稽核紀錄定為 best-effort、R4 退路只涵蓋 D4 的還原寫入。
- 恢復態不呼叫 `_mark_slice_needs_human`，因此 `failed` 不被改寫、`allowed_slice_actions` 不變。

`launch_job_id` 取 `job.get("job_id") if job is not None else None`；`pinned_inputs`／`written_dispatch_base` 取自 D2 的區域變數；`errors.append((slice_id, exc))` 與最後的 `DispatchReadyError` 不變。

### D6 `complete_tick`：未綁定的 launch-failed build job 只供稽核

`manager.complete_tick` 的 build 分支在 `slice_row = _slice_for_job(registry, slice_id, job_id)`（`manager.py:2474`）與既有 reviewer skip（`:2475-2476`）之後、`_repo_root_for_slice_row(slice_row)`（`:2477`）之前加：`if slice_row is None and _is_unbound_launch_failed_build_job(registry, slice_id, job): continue`。新 helper 在下列條件全數成立時才回 True：`job.get("status") == "failed"`、`not _is_workflow_lane_job(job)`、`isinstance(job.get("runtime_diagnostic"), dict)` 且其 `reason == "launch-failed"`、`registry.get_slice(slice_id)` 存在（`KeyError` → False）且其 `builder_job_id != job["job_id"]`。

這是 #497「只有綁定 job 能 finalize」的嚴格子集，唯讀判斷、不新增 persisted 欄位。`runtime_diagnostic.reason == "launch-failed"` 不保證行程從未啟動：`_attach_launch_handle` 失敗（`launch()` 已回傳，`autonomy.py:822`，隨後 `:835-842` 仍呼叫 `_fail_launching_job`）與 `Dispatcher.poll_headless_done` 的 launch handle 缺失也會標這個 reason。這些 job 與本項的 spawn 失敗 job 在 registry 上同形：已是 terminal `failed`，也沒有持久化的 launch handle。skip 的正當性不靠「沒產出」，而靠「已不是 owner」。現行對未綁定 failed build job 只走 `status == "failed"` 分支（`manager.py:2559-2585`），`slice_row is None` 所以不寫 slice，只寫一份覆蓋現任 owner 的 `builder-failed-*` manifest。現任 owner 的 manifest 若是 `passed`／`verified`，`_existing_manifest_job_id` 會回 None 讓它被覆寫（`:191-192`），直接破壞下游 dependency 判定。所以跳過未綁定者只移除這個副作用；仍綁定者（spec R5 e／f）不受影響。一般 `exited`／`failed` 的 superseded job 維持現行（留給 #481／#497），review kind 與 workflow lane 不受影響。launch-failed 分類不觸發 executor backoff，跳過不影響 backoff 記錄；slice lane job 沒有 `workflow_card`，不進 skill ledger（`skill_ledger.append_usage_event` 對此回 None），跳過不影響 usage 記帳。沒有這道 skip 時，D4 還原後的新 failed job 會與仍綁定的舊 reviewer／builder job 輪流覆寫單槽 manifest，每 tick 重跑 `_finalize_review_job` 或 verification runner。

### D7 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| worktree 失敗（帶 candidate branch 的常態） | 沿 `tests/test_coordinator_operator_actions.py` 的 `_create_slice_in_needs_human`／`_Launcher`／`_git_runner` 樣板，另建 exited reviewer job 並設 `reviewer_job_id`、`current_evidence_refs`、`current_evaluation_refs`、`current_verification_evidence_hash`；retry 前改寫 spec 檔內容（模擬 controller 重釘 recovery 指示，使本次 pinned `spec_hash` ≠ 快照 `spec.hash`，對應 #479 recurrence「spec 被 repin 成 recovery-context hash」）；worktree creator 的 `create()` 拋 `ValueError("existing worktree branch has commits outside requested base")`；呼叫 `manager.apply_slice_action(retry-build)` | 拋 `DispatchReadyError`；spec R1 各欄位等於快照；history 長度不變；`actions` 恰多一筆 `dispatch-failed`／`manager`；`allowed_slice_actions` 前後相同且含 `retry-build`；worktree creator 的 `create()` 恰被呼叫一次（證明失敗發生在 repin 之後）；`list_jobs()` 無新 job；`spec.hash` 仍是快照值而非改寫後的檔案 hash（現行 RED：`candidate`／`builder_job_id`／`reviewer_job_id`／refs 被清、`spec.hash`／`dispatch_base` 被改） |
| #479 原始錯誤 | spec 宣告 `executor`／`model_id`、`launcher_factory=None`；另一例 identity 不在 registry | 同上保留（本例失敗在 repin 前，worktree creator 未被呼叫）；錯誤訊息分別含 `launcher_factory is unavailable` 與可用 candidates |
| spawn 失敗 | launcher 的 `launch()` 拋 `RuntimeError`；預寫 handoff manifest（`job_id`＝reviewer job、`gate_status: needs_human`、review evaluation path）；`PSC_REPO_ROOT` 指向 fake repo；`complete_tick` 注入會記錄呼叫的 `verification_runner` | slice 保留（`builder_job_id` 回舊 id）；新 job `status == "failed"` 且 `runtime_diagnostic.reason == "launch-failed"`；`complete_tick` 連跑兩次：新 job 不在 `completed`／`errors`、manifest bytes 不變（`job_id` 仍是舊 reviewer job，未被新 job 的 `builder-failed-launch_failed` 覆寫）、無 `missing-slice-proof`、verification runner 未被呼叫、`actions`／`evidence_history`／`evaluation_history` 長度不變 |
| failed 恢復態 | 以 `_create_slice_in_needs_human` 同一套真實 spec／plan 建 row，再 `update_slice(state="failed", gate_state="failed")`；兩例：`candidate` 為合法 SHA，以及 `candidate=None`（直接以 `create_slice(..., candidate=None)` 建 row，對照 `tests/test_fix_slice_failed_deadend.py`）；皆＋worktree 失敗 | `state`／`gate_state` 仍 `failed`；worktree creator 被呼叫一次；`allowed_slice_actions` 前後相同且含 `retry-build` |
| control request 路徑 | `manager_daemon.build_request_executor` 送 `slice-action` retry-build，monkeypatch `_resolve_launcher` 對 spec identity 拋錯（沿 `tests/test_executor_backoff_slice_lane.py::test_retry_build_request_uses_spec_identity_launcher_factory` 樣板） | 錯誤外拋；slice 保留。CLI `slice-action`（`cli.py:403-415` 經 `_submit_mutation_request`）與 porcelain `recover slice`（`porcelain/recover.py:117-120`、`:165-175` 經 `control_client.submit_request`）都只送出 `slice-action` control request，不直接碰 registry；伺服端唯一執行點就是本列的 `build_request_executor` → `apply_slice_action`（`manager_daemon.py:926-956`），所以本列同時是兩個入口的失敗保留驗收 |
| 退路 | (i) worktree creator 在拋錯前先 `registry.update_slice(slice_id, candidate="c" * 40)` 模擬 repin 後的並行寫入；(ii) monkeypatch `registry.repin_slice` 讓第二次呼叫（還原步驟）拋錯；(iii) worktree creator 在拋錯前先 `registry.record_action(slice_id, action="concurrent-evidence", actor="other", evidence_refs=["new-evidence.json"])`（不動四個綁定欄位、只改 refs） | 三例都走 `_mark_slice_needs_human`（`state == "needs_human"`）；(i) 的 `candidate` 維持 `"c" * 40`、(iii) 的 `current_evidence_refs` 維持 `["new-evidence.json"]`，都不被 prior 覆寫 |
| 修正後重試 | 先觸發 worktree 失敗，再換正常 creator 重送 retry-build | 第二次成功：新 job、`building`／`pending`、`candidate is None`、refs `[]` |
| 行為不變 | 首次派工失敗、`pending` row 失敗、成功路徑、backoff skip、仍綁定的 launch-failed job | 與現行斷言逐字相同（#333-1 `dispatch_base`、`builder-failed-*`、backoff skip 無副作用） |

### D8 Sizing

2 個 production 模組（`autonomy.py`、`manager.py`）→ `domain_breadth=1`；寫入只觸及單一 slice row 的既有欄位、全經既有已驗證 writer、不新增 durable 欄位、無跨物件 CAS，`complete_tick` 只加唯讀 skip → `state_consistency=1`；三件齊全時機械三維固定 4，總分 6／Yellow。
