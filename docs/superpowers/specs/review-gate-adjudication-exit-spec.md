---
status: accepted
work_item: review-gate-adjudication-exit
---

# Review gate `blocking-findings` 的 operator 裁決出口規格

## Requirements

對應 [#956](https://github.com/hamanpaul/paulsha-cortex/issues/956)：review 卡的 gate 只要有一條 finding 落在 `BLOCKING_FINDING_CATEGORIES` 就判 `rejected`，run 轉成 `blocking-findings` needs_human。此後 operator 的裁決沒有入口可以送回 review gate：`retry-review` 拒收 `reason`，`retry-card` 拒絕已採信的卡，`review-attest` 只在 ship 段生效，`next_actions` 也只列 `abandon`。本票補上「接受」（`retry-review --reason`）與「駁回」（`retry-build --reason`）兩個裁決出口，讓曝光面列得出這兩個動作，並讓 `review-attest` 在這個情境明確拒絕，不再回報假成功。review gate 本身的判準（`review.py`）不改。

1. **R1 `retry-review` 接受 `reason`**：`_retry_review_action` 的 allowlist 加入 `reason`。驗證用既有的 `_validate_operator_adjudication_args(args, state_path=..., action="retry-review")`，放在 allowlist 檢查之後、讀取 run 與任何狀態變更之前。以下情形拋 `ValueError` 且不動 run：非字串、空白、長度超過 `OPERATOR_ADJUDICATION_REASON_LIMIT`（4000），訊息為 `retry-review reason must be a non-empty string within 4000 characters`；帶了 reason 但 `state_path is None`，訊息含 `retry-review reason requires a durable state path`。`_retry_review_action` 的簽名新增 keyword 參數 `state_path: Path | None = None` 與 `now_epoch: float | None = None`。`execute_work_action` 呼叫它時傳入 `resolved_state_path` 與 `now_epoch`，做法與 `retry-card` 相同。
2. **R2 evidence 落地與收據**：registry reset（`_manager_reset_workflow_for_retry_review`）成功後，以 `_record_operator_adjudication` 寫一筆 `cortex-operator-adjudication/v1` evidence，欄位如下：
   - `card`：reset **之前**的 run 上 `manager._current_workflow_step(run)` 的 card，也就是被判 rejected 的那張 review 卡；若該值為 `None`，改取 run 上最後一張 `phase == "review"` step 的 card；連 review step 都沒有時用字面值 `"review"`。
   - `phase`：`"review"`。
   - `reason`：strip 後的字串。
   - `actor`：缺省時為 `"operator"`。
   - `created_at`：由 `now_epoch` 導出。

   回傳新增兩個鍵：`adjudication_evidence`（`{"ref", "hash"}`）與 `adjudication`（`operator_adjudication_receipt(evidence, card=<該 card>)`）。reset 本身若拋例外（Candidate CAS mismatch、缺 frozen plan authority、有 active job、verify phase 未完成），一律不寫 evidence。未帶 reason 時不寫 evidence，兩個新鍵的值為 `None`；其餘回傳欄位（`action`、`reason: "foreign-review-rerun-dispatched"`、`expected_candidate`、`run`、`retry_classification`）與 registry reset 行為維持現狀。
3. **R3 裁決進入重派 reviewer 的 prompt**：R2 寫出的 evidence 由既有的 `manager._operator_adjudications(run, coordinator_root)` 讀回。重派的 review 卡（`code-review`／`adversarial-review`）prompt 必須包含 `OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE`，以及該 reason 的前 `RETRY_CONTEXT_EVIDENCE_LIMIT`（2000）字，逐字不改。reason ≤2000 字時全文進 prompt；超過 2000 字時第 2001 字以後只留在 evidence 全文，不進 prompt。這是既有讀取端 `_operator_adjudications` 的截斷（manager.py:10022），retry-card／retry-build 的裁決也一樣。讀取端與 dispatch 接線（`_dispatch_workflow_card` 內 `operator_adjudications=_operator_adjudications(run, coordinator_root)`）一字不改。
4. **R4 reviewer directive 補上「已接受的 finding 不得再以 blocking 類別回報」**：`OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE` 保留既有文字，句尾再附加一句：某條裁決若明示接受或豁免某個 finding／偏離，reviewer 不得再以 blocking 類別回報該 finding；若仍要記錄，只能用 non-blocking 類別，並在 `recommendation` 引用該裁決。句中的 blocking 類別清單由 `foreign_review.BLOCKING_FINDING_CATEGORIES` 排序後機械導出，non-blocking 類別清單由 `foreign_review.VALID_FINDING_CATEGORIES - foreign_review.BLOCKING_FINDING_CATEGORIES` 排序後導出，不得手寫。`OPERATOR_ADJUDICATION_DIRECTIVE`（builder 側）與無裁決時的 prompt 必須逐字不變。
5. **R5 `next_actions` 列出兩個出口**：`_phase_recovery_actions` 只在下列條件全部成立時補宣告：`current_phase == "review"`、`status == "ongoing"`、facets 含 `needs_human`、`needs_human_reason.reason == "blocking-findings"`、job 清單讀取成功，且該 run 沒有處於 `ACTIVE_JOB_STATUSES` 的 job。`workflow_registry.list_jobs()` 拋例外時，無法確認有沒有 active job，所以兩者都不宣告（fail-closed）。既有的 `regenerate-gates`／`retry-card` 宣告需要正向的 job 證據，讀取失敗時本來就不會出現，行為不變。各動作另有前置條件：
   - **`retry-review`**：`candidate_head` 是字串且等於 `verified_head`；`planning_authority` 含 `kind == "plan"` 的項目；verify phase 的 step 非空且全數 `passed`。這與 `_retry_review_action`／`_manager_reset_workflow_for_retry_review` 是同一組前置條件。
   - **`retry-build`**：`candidate_head` 是字串；build phase 的 step 非空且全數 `passed`；`passed` 的 ship step 至多一張，而且只能是 Manager-owned `openspec-archive`（`executor == "cortex-manager"`、`model == "deterministic"`、`domain == "cortex"`）。這與 `_manager_reset_workflow_for_retry_build` 的 review-phase 分支是同一組前置條件。

   宣告順序：既有 extras 在前，後面依序是 `retry-review`、`retry-build`；已存在的不重複列。其他 reason（含 `copilot-*`）以及其他 phase 的宣告結果維持不變。
6. **R6 `next_step_hint`**：新增純函式 `work_actions.blocking_findings_next_step_hint(*, work_id, repo, candidate) -> str`，輸出 zh-tw，逐字列出兩條指令，並註明兩者都不適用時才 `abandon`：
   - 接受：`cortex run work retry-review <work_id> --repo <repo> --expected-candidate <candidate> --actor <operator> --reason '<裁決>'`
   - 駁回：`cortex run work retry-build <work_id> --repo <repo> --expected-candidate <candidate> --actor <operator> --reason '<裁決>'`

   參數不合格式時改用佔位字串：`work_id` 須符合 `[a-z0-9][a-z0-9-]*`，否則為 `<work-id>`；`repo` 須為 `owner/repo`，否則為 `<owner/repo>`；`candidate` 須為 40-hex，否則為 `<candidate-sha>`。使用點有兩處：
   - (a) `_claim_action`（resume）的回應：補上的 extras 含 `retry-review`，且 `canonical_run.needs_human_reason` 的 reason 為 `blocking-findings` 時，以本函式輸出覆寫 `next_step_hint`。
   - (b) `manager.workflow_status_entry`：最終的 `next_actions` 含 `retry-review`、`reason_code == "blocking-findings"`，且 needs_human_reason 沒有 persisted `next_step_hint` 時，以本函式輸出取代通用的 abandon 提示。
7. **R7 `review-attest` 不再回報假成功**：`_review_attest_action` 在 `_load_work_run` 之後、寫入 maintainer evidence 之前加一道檢查：run 上任一 `phase == "review"` 的 step 若 `gate_result == "needs_human"`（review gate rejected 的持久標記），就拋 `RuntimeError`。訊息須含 `review-attest does not adjudicate review-gate blocking findings`，並指向 `retry-review --reason`（接受）與 `retry-build --reason`（駁回）。此時不寫 `cortex-maintainer-review/v1` evidence，不改 run 的 `gate_refs`／`facets`。其他情境維持現行行為，包括 review step 全為 pending／passed 時的 exact-HEAD attestation，以及 ship 段 `copilot-*` stop 的 maintainer 重入。
8. **R8 CLI help**：`paulsha_cortex/coordinator/cli.py` 中 `work` 子命令 `--reason` 的 help 改寫為兩組：
   - `retry-build／retry-card／retry-review`：operator 裁決，最多 4000 字，全文落成 operator-adjudication evidence，前 2000 字注入之後的 dispatch prompt。
   - `abandon／retire-delivered／recover-superseded／reset-reclaim-budget／refreeze-base`：單行審計理由，最多 500 字。

   `python3 -m paulsha_cortex.cli work retry-review --help` 須 exit 0，輸出含 `4000`。
9. **R9 測試**：新增 `tests/test_review_gate_adjudication_exit.py`，走 RED→GREEN，涵蓋下列情境：
   - (a) `retry-review` 帶 reason：evidence 寫在 `<state_path.parent>/evidence/operator-adjudication/<run_id>-<hash>.json`，權限唯讀，body 的 `schema`／`card`（等於被 rejected 的 review 卡）／`phase == "review"`／`reason`／`actor` 正確；回傳的 `adjudication_evidence.ref` 指向該檔；`adjudication.next_step_hint` 含 card 名；run 的 review step 回到 pending，needs_human 已清除。
   - (b) 非法 reason（`"   "`、`"x" * 4001`、`123`）拋 `ValueError`；run 的 facets 與 review step `gate_result` 不變，`evidence/operator-adjudication` 目錄不存在。
   - (c) 合法 reason 但 Candidate CAS mismatch：拋 `RuntimeError`，不寫 evidence。
   - (d) 不帶 reason：不寫 evidence，兩個新鍵為 `None`。
   - (e) 端到端：接續 (a)，`manager._operator_adjudications(run, state_path.parent)` 回傳的 rows 餵給 `manager._workflow_job_prompt`（reviewer persona 的 review step），prompt 含 reason 逐字與 `OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE.strip()`。另跑一個 2500 字、尾段 500 字帶獨特標記的 reason：evidence body 的 `reason` 保留 2500 字全文；prompt 含前 2000 字，不含尾段標記。
   - (f) directive：每個 blocking 類別名與每個 non-blocking 類別名都出現在 `OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE`；`OPERATOR_ADJUDICATION_DIRECTIVE` 不含新增的那一句。
   - (g) `_phase_recovery_actions`：blocking-findings 的 review run 同時含 `retry-review` 與 `retry-build`，且 `retry-review` 排在前面；reason 不是 blocking-findings 時兩者都不宣告；有 active job 時兩者都不宣告；`list_jobs()` 拋例外的 registry 兩者都不宣告；`verified_head != candidate_head` 或缺 plan authority 時只宣告 `retry-build`。
   - (h) hint：`blocking_findings_next_step_hint` 含兩條指令與實際的 work_id／repo／candidate，非法輸入改為佔位字串；`manager.workflow_status_entry(registry, run)` 與 `execute_work_action(action="resume")` 的回應中，`next_actions ⊇ {abandon, retry-review, retry-build}`，且 `next_step_hint` 等於該函式輸出。
   - (i) 對 review step `gate_result == "needs_human"` 的 run 執行 `review-attest`：拋 `RuntimeError`，不產生 `evidence/maintainer-review`，run 的 `gate_refs`／`facets` 不變。
   - (j) coordinator `work` parser 的 `--reason` help 字串含 `retry-review` 與 `4000`。

   下列既有測試的原斷言保留、全綠：`tests/test_work_actions_retry_invalidation.py`、`tests/test_wiring_retry_sizing_recompute.py`、`tests/test_operator_adjudication_752.py`、`tests/test_adjudication_builder_prompt_814.py`、`tests/test_reviewer_card_retry_569.py`、`tests/test_work_actions.py`、`tests/test_copilot_review_adopt_existing.py`、`tests/test_workflow_production_wiring.py`、`tests/test_coordinator_cli_flags.py`。

## Boundary

Production 只改三個模組：
- `paulsha_cortex/coordinator/work_actions.py`：`_retry_review_action`、`execute_work_action` 的 retry-review 分支、`_phase_recovery_actions`、`_claim_action` 的 needs_human 回應、`_review_attest_action`，以及新增的 `_blocking_findings_recovery_actions`／`blocking_findings_next_step_hint`。
- `paulsha_cortex/coordinator/manager.py`：`OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE`、`workflow_status_entry`。
- `paulsha_cortex/coordinator/cli.py`：只改 `--reason` help 文字。

不改的範圍如下：
- review gate 判準：`review.py` 的 `BLOCKING_FINDING_CATEGORIES`、`_normalize_finding`、`validate_review_verdict`，以及 `manager.apply_workflow_action` 的 rejected 分支（manager.py:12671-12713）。不做 #956 建議 2（依 severity／recommendation 改判）。
- `review-attest` 不擴成 review 卡 gate 的採信來源（#956 建議 3），`work_bridge.py` 的 ship maintainer 路徑也不動。
- `retry-card` 的 accepted-evidence 拒絕規則、`registry.py` 的三支 reset、`control/contract.py`：已核對，contract 對 retry-review 只驗 `expected_candidate`，不限制 `reason`。
- `manager._operator_adjudications` 的讀取上限：每筆 reason 截成 `RETRY_CONTEXT_EVIDENCE_LIMIT`（2000）字、每個 run 最多取 3 筆，本票不改。調高上限會同時改變 retry-card／retry-build／retry-review 三個寫入端的 prompt 大小。evidence 本身保留 ≤4000 字全文供稽核。
- `claim.needs_human_next_actions`、porcelain `run.py`／`recover.py`、`paulsha_cortex/cli.py` 的 `_WORK_HELP`。
- slice lane（`SLICE_ACTIONS`／`slice-action retry-review`）。
- 下列鄰近議題另有 issue 追蹤，本票不處理：#935（ship 段 needs-fix 的 disposition 續行）、#953（operator ruling 生命週期）、#546（recovery helper 抽共用與 `regenerate-gates` 曝光面）。

## Evidence

- **Issue #956**（2026-09-23，pin `442fe23f`，run `workflow-5fb6da3bc6d2cfc3c3ed`，對應 #948）：兩輪 code-review 都回 `status: passed`，但各帶一條 minor finding，其中一條是 `scope-bypass`，reviewer 在 recommendation 自寫「confirm with the operator」。gate 仍判 `rejected` → `blocking-findings`。operator 的裁決是「全部接受」，但沒有入口：`review-attest` 回報成功，phase 卻不動；`retry-card` 拋 `retry-card refuses a card with accepted evidence`；`retry-review --reason` 拋 `retry-review rejects caller evidence/input: reason`；`next_actions` 只有 `abandon`。最後只能場外 merge PR #952 再 `retire-delivered`。
- **main `7fa4716b` 上的 `work_actions.py`**：
  - `_retry_review_action`（2867-2931）的 allowlist（2875-2879）沒有 `reason`；`execute_work_action` 呼叫它時（6401-6406）沒有傳 `state_path`／`now_epoch`。對照組 `retry-card` 在 6386-6394 有傳。
  - `_retry_card_action` 在 2762 拒絕已採信的卡。
  - 裁決通道的既有實作：`_record_operator_adjudication`（2297-2336）、`operator_adjudication_receipt`（2339-2358）、`_validate_operator_adjudication_args`（2361-2377）。retry-build 在 2433 呼叫 validator、2504 寫 evidence；retry-card 在 2688 呼叫 validator、2784 寫 evidence。
  - `_phase_recovery_actions`（2522-2599）只補 `regenerate-gates`／`retry-card`，以及 `copilot-*` 時的 `review-attest`（2597）。2560-2563 在 `list_jobs()` 拋例外時退回 `jobs = []`，後面的「沒有 active job」判定因此會成立。
  - `_review_attest_action`（1277-1380）只檢查 `current_phase == "review"` 與恰好一個 foreign-review ref，不看 review step 的 `gate_result`；它在 1378 清掉 `needs_human`，但 review step 仍停在 `needs_human`。
- **main `7fa4716b` 上的 `manager.py`**：
  - rejected 分支：12678 把該卡 `gate_result` 寫成 `"needs_human"`；12681-12713 落 `blocking-findings`。
  - 讀回與注入：`_operator_adjudications`（9981-10027）讀 run 級 evidence，在 10022 把每筆 reason 截成 `RETRY_CONTEXT_EVIDENCE_LIMIT`（9772，2000 字），dispatch 在 11249 注入每一張卡。
  - `OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE`（9789-9794）沒有「已接受的 finding 不得再以 blocking 回報」的語意。
  - `workflow_status_entry`（1010 起）的 hint 在 1068-1074 退回通用的 abandon 提示。
  - operator `resume` 在 11812-11824 只會無裁決地重派 reviewer。
- **`review.py`**：`BLOCKING_FINDING_CATEGORIES`（56-66）含 `scope-bypass`；`_normalize_finding` 在 702 以類別決定 `blocking`；`validate_review_verdict` 在 785 的判定是「任一 blocking 即 rejected」。
- **`claim.py`**：`needs_human_next_actions`（1264-1290）對 review phase 只回 `("abandon",)`。
- **`work_bridge.py`**：maintainer-review ref 只在 2148-2150 由 ship 路徑讀取。
- **`coordinator/cli.py`**：`--reason` help（232-238）只列 abandon 族。
