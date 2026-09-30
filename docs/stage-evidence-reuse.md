---
contract_version: stage-evidence-reuse/v1
issue: 844
---

# Production stage-evidence reuse

本文件描述 Manager 在 workflow 的正式接續路徑上，什麼情況可以不再呼叫模型、直接採信某張 verify／review 卡既有的成功結果，以及每次決策留下的 receipt。本票是 #214 的有界 successor。支援 cohort 與「未支援」清單供 #829 總帳引用。

## 入口與決策點

- 正式入口是 `cortex work resume <work-id> --repo <owner/repo>`：work-action 經 daemon 進入 `manager.resume_workflow_run`。Manager periodic tick 的 workflow resume 迴圈也呼叫同一個函式。兩者共用同一個決策點 `_workflow_stage_reuse_probe`。
- `work-action resume` 只接受 `action`／`repo`／`work_id`／`issue`；`workflow-action resume` 只接受 `action`／`run_id`。夾帶 key 或 evidence 的請求直接拒絕。
- legacy `workflow-action/start` 仍會讀 caller 帶來的 `stage_execution_key`。production executor factory 沒有注入 validator，所以這把 key 永遠不會讓派工短路。caller key 不是 admission authority。
- 明確的 `retry-card`／`retry-build`／`retry-verify`／`retry-review` 走 `force_new_card`，一定建立新 attempt，不受相同 key 影響。

## 支援 cohort

目前只支援 verify／review phase 的 reviewer 卡（`STAGE_EXECUTION_REUSE_SUPPORTED_PHASES`）。以現行 deck 列出卡名：

| phase | card | persona |
|---|---|---|
| verify | `verification` | reviewer |
| review | `code-review`、`adversarial-review` | reviewer |

以下條件全部成立，probe 才會判定 `reused`：

1. 來源 job 屬於同一個 run、同一個 claim-era，且是這張卡在 exact candidate 上最新的一顆 job，狀態為 `exited`、exit code 0。`run_id` 與 `claim_key` 都算進 key，跨 run 或跨 era 的 key 在雜湊層就不可能相同。
2. Manager 以現在的狀態重算的 stage execution key（schema v3）等於來源 job 落盤的 key。key 涵蓋 repo、work、run、claim key、card、phase、executor、model、base（`source_revision`）、candidate、frozen planning authority hashes、action、test policy、#835 execution profile resolved key、builder job、verify 卡的 Manager gate ledger，以及 run 級 operator 裁決。execution profile resolved key 又涵蓋 native effort、adapter runtime version、loadout、toolset、sandbox、permissions、toolchain。
3. 來源 job 的 provenance 成立（`_stage_reuse_provenance_mismatch`）：receipt 是現行 schema，receipt 的 key 就是 job 的 key，逐欄與現在的快照相同，而且 job 實際記錄的 executor／model 就是快照上的 identity。只抄了一把相同 key 的 job 不算。
4. 來源 evidence 仍能通過 production reader `_read_job_workflow_evidence`（locator、檔案 hash、symlink、越界路徑、artifact drift）與 domain validator（verification evidence，或 foreign review 的 builder／reviewer identity 與 independence）。

pricing 等 metadata 不在 profile key 內，改動時不會讓 reuse 全面失效。

## 採信與 receipt

reuse 分兩步完成：

1. **決策**：probe 判定 `reused` 時，Manager 只記下來源 job，不預寫任何 receipt。
2. **採信**：`apply_workflow_action(action="advance")` 從 registry 重讀 run，在這份快照上呼叫 `_stage_reuse_adoption_receipt` 重驗。它重新確認 provenance，並重算所有 run 衍生的輸入（candidate、claim-era、planning authority、test policy、builder job、gate ledger、operator 裁決）。executor、model 與 execution profile 沿用同一次呼叫內 probe 剛解析出的值。任何一項不符就擲 `StageReuseAdoptionDrift`：這次接續結束，不寫 gate、不落 needs_human，receipt 記成 `ineligible`／`adoption-drift`。下一次接續以新狀態重新裁決，通常是 stale，改派新 attempt。重驗通過後，receipt 與 gate 在同一次 `_manager_update_workflow_run` 寫入，受 registry revision CAS 保護。

`WorkflowRun.stage_reuse_receipts[<card>]` 的第 2 版欄位：

| decision | 欄位 |
|---|---|
| `reused` | `receipt_schema_version=2`、`stage_execution_key`、`stage_execution_key_schema_version=3`、`compatibility=exact-stage-execution-key`、`phase`、`candidate_sha`、`source_run_id`、`source_claim_key`、`source_job_id`、`source_evidence_path`（相對 coordinator root）、`source_evidence_hash`（evidence 檔 sha256）、`adoption`（`accepted`，或 review verdict 有阻擋 finding 時為 `rejected`） |
| `fresh` | `receipt_schema_version=2`、`stage_execution_key`、`job_id`（新 attempt）；因不相容改派時另有 `mismatched_fields` |
| `ineligible` | `receipt_schema_version=2`、`reason`，以及選填的 `superseded_key`、`mismatched_fields` |

`ineligible` 的 `reason`：

| reason | 意義 |
|---|---|
| `no-eligible-candidate`／`launcher-unavailable`／`probe-exception`／`profile-unresolvable` | 來源 job 帶 key，但現在綁不出可比對的 identity 或 profile。視同不可重用，改派新 attempt。 |
| `adoption-drift` | 決策與採信之間 run 衍生的輸入改變了。 |
| `stale-dispatch-failed`／`stale-dispatch-produced-no-job` | 已判定不可沿用，但新 attempt 派不出去（例如沒有 foreign reviewer），或派工回傳非 Job 決策。 |
| `authority-restart` | authority 前進，新的 claim-era 不沿用前代 era 的 evidence。 |

receipt 只是 provenance，不是 admission authority。每次 gate 都照常以 production reader 與 domain validator 重新驗證 evidence。CompletionRecord 的 `reused_from` 同樣只是 provenance，`completion.read_completion_record` 仍會完整驗證 evidence 參照。

## 什麼時候會出現 `reused`

正常流程中，resume 在同一次呼叫內 poll 到 job 結束後就直接採信，receipt 維持派工時寫的 `fresh`。要看到 `reused`，必須有一個後來的決策點遇上「job 已經是 `exited`／0，gate 仍是 pending」。例如：

- job 在 workflow resume 之前就被其他路徑 finalize，像是 `run_tick` 的 in-flight poll：periodic tick 的後半段或手動 `cortex tick`；
- job 結束且已被 finalize 成 `exited` 之後、採信之前 Manager crash 或重啟。job 行程結束但 registry 仍是 `dispatched`／`running` 時重啟，重啟後的 resume 會在同一次呼叫內 poll、finalize、採信，receipt 仍是 `fresh`（2026-09-30 live 實測）。要在 live 重現，可在 job 結束後、下一次 tick 之前執行 `cortex run complete`，先讓 job 收割成 `exited`；
- 採信暫時失敗（例如 rate limit），之後再接續。

## 呈現

- resume 的 action result 帶 `stage_reuse`：`{"card": ..., <receipt>}`。`work-action` 的 result 另含 `run.stage_reuse_receipts`。
- `cortex work show [--json]` 帶 `stage_reuse`：`{"run_id", "cards": {card: receipt}}`。文字模式每張卡印一行 `stage_reuse[<card>]: decision=...`。
- authority restart 讓已接受的 verify／review gate 回到 pending 時，`cortex work resume` 的結果帶 `stage_invalidation`：原因、前後 claim key、前後 source revision、candidate、需重跑的卡、保留的 build 卡。同一次寫入也把這些卡的 receipt 標成 `ineligible`／`authority-restart`。

## Migration 與相容

- 第 1 版 receipt（`{"decision", "stage_execution_key"}`，沒有 `receipt_schema_version`）照舊可讀，不回填任何欄位。
- 第 2 版的新欄位只在出現時才驗形狀。目前部署的 runtime pin（#1129 merge）的 `stage_reuse_receipts` 驗證會容忍多出的欄位，rollback 時讀新 receipt 不會 fail closed。
- job 上其他 key schema 版本的 `workflow_stage_execution_receipt`（升版前或 rollback 後寫下的）只驗最小形狀，不會讓 registry 讀取失敗。probe 以 `schema_version` 不符判定 stale，一律改派新 attempt。
- 沒有 key 的 legacy job 維持 #844 之前的行為：沿用既有的 `jobs[-1]`，不寫 receipt、不補值。

## 未支援

以下情況不在本票的安全 cohort 內。#829 ledger 應維持「未支援」：

- build 卡（`worktree-isolation`、`tdd-red`、`subagent-build` 等）。它們的輸出就是 candidate，由既有 job checkpoint 去重。
- planner 卡（define／plan）與 manager 卡（ship）。
- 跨 run、跨 claim-era 的 evidence 採信或 rebinding。原 evidence 的 run／claim 綁定不改寫。authority restart 保留已接受 build candidate 的既有政策不變，也不擴大成 stage reuse。
- 無法由現行 domain validator 重新驗證的 artifacts。
- 兩個 process 對同一個 coordinator root 並行 resume 同一個 run。這不是支援的拓撲；daemon 本身逐一處理請求。registry revision CAS 保證只有一方寫入成功。後到的一方若發現這張卡已被採信，會回傳 `duplicate-continuation`；其他情況維持既有的 fail-closed。

## 驗收對照

S01–S11 的自動化證據在 `tests/test_workflow_stage_execution_reuse_844.py` 與 `tests/test_workflow_stage_reuse_adoption_844.py`（依 S 編號分組）。S12 需要在已載入修正版的 runtime 上跑一張受治理的 canary，依上方「什麼時候會出現 `reused`」刻意安排：

1. verify job 行程結束後、下一個 periodic tick 之前，送一次 `cortex tick --specs-dir <空目錄>`，由 `run_tick` finalize 這顆 job，但不推進 workflow。
2. 接著 `cortex work resume`，或等下一個 periodic tick。
3. 在 resume 結果、`run.stage_reuse_receipts`、`cortex work show --json` 中確認以下幾點：verification 卡為 `reused`，`source_job_id` 是該 job、`adoption=accepted`；verification job 數與 model invocation 數沒有增加；gate 前進到 review，並派出 code-review 卡。
