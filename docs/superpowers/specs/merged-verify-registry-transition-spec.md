---
status: accepted
work_item: merged-verify-registry-transition
---

# verify-reset merged run 的受限 registry terminal transition 規格（#976）

## Requirements

### 背景與切片界線

父票 [#887](https://github.com/hamanpaul/paulsha-cortex/issues/887) 的 Child B 為 [#962](https://github.com/hamanpaul/paulsha-cortex/issues/962)。#976 是 #962 的 registry transition 切片，依賴 [#975](https://github.com/hamanpaul/paulsha-cortex/issues/975) 的 admission／proof oracle contract；#975 先依賴 #961 合併。順序是 #975 → #976 → [#977](https://github.com/hamanpaul/paulsha-cortex/issues/977)。

#976 只在 paulsha_cortex/coordinator/registry.py 新增一條私有、受限的 Manager-only transition。它不讀 GitHub、journal、authorization 或 CompletionRecord 檔案；不實作 #975 proof；不建立 CompletionRecord／shipped outcome、不修改 Manager finalizer（#977）。#962 與 #887 的完整 AC 不因本切片完成而縮減或關閉。

### R1 — 專用 manager-only API，不放寬通用轉移

JobRegistry 新增私有 API，建議名稱為 _manager_complete_merged_verify_reset_workflow_run(run_id, *, expected_candidate_head, authorization_hash, terminal_binding)。首次寫入只能從精確的舊版 merged run verify-reset state 進行；既有終態僅可依 R6 作 exact no-write re-entry，且不表示該 row 由本 API 建立。保留 _manager_update_workflow_run() 的參數、權限與 phase-transition 行為不變；不放寬 validate_workflow_phase_transition()，不建立 public/general-purpose verify→ship 或 verify→done API。

此為 registry-local transition gate，不是 external proof verifier。只有 #977 的 Manager finalizer 在先完成 #975 admission/proof 並取得已驗證 CompletionRecord 後，才會呼叫此 API。Python underscore 是內部 API 邊界，不得增加 CLI 或其它 user-facing 呼叫入口。

### R2 — 完整且精確的 terminal binding

呼叫端必須同時提供非空 exact run_id、expected_candidate_head、由 #975 proof oracle 得到的 authorization_hash，以及以已驗證 CompletionRecord 建立的完整 terminal_binding。terminal_binding 使用精確欄位集合，不接受缺欄、額外／形狀錯誤欄位或 partial update：

- identity：run_id、repo、work_id、candidate_head。
- authorization_hash：從 validated CompletionRecord 的 work_authority.trusted_evidence_refs 中唯一 merge_authorization hash 投影而來；必須與此 API 的 authorization_hash（由已通過 #975 proof 的同一授權 hash）完全一致。
- Manager 已驗證的 steps 與 gate_refs，作為 WorkflowRun 現有 WorkflowStep 與 GateEvidenceRef tuple；這些是此方法寫入的資料，不代表 registry 重新驗證其外部證據。
- 完整 completion 欄位：completion_record_path、completion_record_hash、completion_record_revision、completion_source_revisions、pr_candidate、merge_revision。

格式須嚴格驗證：expected_candidate_head、binding.candidate_head、completion_record_revision、pr_candidate 必須是相同的合法 40-hex candidate SHA；merge_revision 是合法但可不同的 40-hex merge commit；record hash 與 authorization hash 為 64-hex digest；path、repo、work_id 非空；source revisions 為非空 string map；steps/gate refs 具有正確型別、完整 phase/card spine、唯一 gate identity。binding 的 identity 必須逐欄等於 registry 目前 run。不得從缺欄位猜值、沿用部分 caller 值或建立新 persisted schema 欄位。

registry 不讀 terminal_binding 所指檔案、不計算／核對檔案 hash、不驗證 journal/GitHub/PR/authorization bytes。authorization hash 的可信來源與 CompletionRecord 有效性由 Manager caller 的前置 proof/validation 保證；此 API 只比對 #975 proof hash 與 CompletionRecord binding 中帶來的 hash，並核對 registry-local run state。

#977 是此 private API 唯一的產品 caller：每次呼叫（含重試）都須重新取得 #975 的內部 proof hash，並以 `completion.read_completion_record(path, expected_hash=...)` 重讀、驗證 CompletionRecord 及其引用證據。identity、candidate、唯一 `merge_authorization` hash、merge commit 由 normalized record 投影；path/hash 來自可信 CompletionRecord writer result；`completion_record_revision` 與 `pr_candidate` 必須等於 candidate；`completion_source_revisions` 必須沿用現有 adapter 的精確投影：`{value.rsplit("@", 1)[0]: value.rsplit("@", 1)[1] for value in normalized["work_authority"]["source_revisions"] if "@" in value}`。重試時 current WorkAuthority 僅供 proof/identity 驗證，不可重算或取代 frozen record source map。`merge_revision` 同時須等於 trusted remote-closure result 與 record 的 `work_authority.merge_commit`。registry 僅比對傳入值；它不自行證明 caller、record 或 proof 真實。

### R3 — 只接受 exact ongoing verify-reset run

首次轉移時，registry 當下的同一 run 必須同時滿足：run_id 精確相同、status=ongoing、current_phase=verify、retry_classification=authority_restart、candidate_head=expected_candidate_head。terminal_binding 的 run_id/repo/work_id/candidate_head 也必須和目前 run 一致；任一狀態 drift、head/auth hash mismatch、run 已 superseded、一般 verify run、review/build/ship run 或一般 ongoing run 都拒絕。既有 ship/done run 只能進 R6 同值、零寫入分支；該分支不聲稱也不能證明終態由哪條 writer path 建立。

本 API 不負責辨認 authority conflict 或重新判定 run 是否應走 completion；#975／Manager finalizer 已決定且 proof 成功的舊 verify-reset run 才能呼叫。不得因 claim key 相同、journal 字串、PR closed 或 caller 自稱 trusted 而放寬以上 gate。

### R4 — active job 檢查與寫回為同一同步 registry mutation

在同一個同步 method 呼叫內，緊鄰 WorkflowRun 構造與 persist 前，檢查 self._jobs 中 workflow_run_id 等於本 run_id 且 status 屬 dispatched/running 的 job。存在任一 active job 即拒絕。不得在檢查後呼叫外部 callback、await、yield 或 dispatch，再寫入 terminal state；狀態檢查、new WorkflowRun 構造、暫存 registry 更新及 persist 必須在同一同步 mutation 中完成，避免接納另一 run/job generation。

### R5 — 成功只寫入既有 ship/done schema

通過所有前置後，一次建立並持久化合法 WorkflowRun：current_phase=ship、status=done、verified_head=expected_candidate_head、gate_status=passed；帶入完整 Manager-verified gate_refs、steps 及 R2 completion fields；清除 needs_human facets/reason。保留 run/work/repo/claim identity、candidate_head、attempts、source authority、retry_classification=authority_restart 及其它未列明欄位。不得部分寫入、重建另一個 run、修改 jobs、增 schema/欄位或把無 proof 的 gate/step 改成 passed。

首次轉移更新既有 `updated_at` 欄位為該次轉移時間；R6 同值重入不重新蓋時間戳。

所給 steps 必須維持原 workflow manifest 的 phase/card/persona identity，不得改骨架；只有 Manager 已驗證的 terminal step evidence 可反映為 passed。所給 gate_refs 必須符合現有 WorkflowRun ship invariants：foreign-review、brainstorm（若該 run 要求）、且恰一種 current-HEAD delivery review（copilot 或 maintainer-review）；ship 需要 verify/review/ship phase steps 全 passed、verified_head 等於 candidate，並維持 builder/reviewer independence domain 分離。WorkflowRun 既有 __post_init__ 驗證須照常執行。

verify→ship 是此私有 API 唯一特例：它不得呼叫一般 phase validator，也不改該 validator；僅直接構造受現有 WorkflowRun schema/invariants 約束的 terminal row。

### R6 — Exact idempotent re-entry，拒絕終態覆寫

若同一 run 已是 status=done、current_phase=ship，且 retry classification、run/work/repo identity、candidate/verified head、gate status、steps、gate refs、needs-human facets/reason、所有 persisted CompletionRecord 欄位與本次 terminal binding 逐欄完全相等，並且本次 Manager caller 已重新完成 R2 的 #975 proof 與 record 驗證、API 的 authorization_hash 與 record 投影 hash 相等，回傳目前 run 的 copy，不呼叫 `_persist()`。既有 `updated_at` 必須是合法非空時間值，重入時保持原值；只有首次由 ongoing 轉 terminal 時才將 `updated_at` 設為該次轉移時間。

WorkflowRun schema 不新增 authorization_hash 或 transition-origin 欄位。現有 persisted fields 無法分辨完全相同的 done/ship row 是由此 restricted API 或 ordinary path 寫出；不得推斷來源、宣稱 provenance，或以「不是此 API 建立」為拒絕理由。安全邊界是每次重入都由 trusted Manager caller 重新核對 proof hash 與 validated CompletionRecord hash，再以完整 terminal field equality 保證不覆寫不同值。任一 identity/head/auth-bound/record/gate/step 欄位不同、存在 active job、或 caller 無法重新驗證 binding，均拒絕且不寫。

### R7 — Failure atomicity

任何欄位、identity、狀態、active-job、proof-hash 對應或 expected-state 檢查失敗，須在變更 registry memory/file 前拒絕。若 WorkflowRun 建構或 _persist() 失敗，memory 的 run/jobs snapshots 與 durable jobs.json 必須保持 transition 前狀態；傳遞原失敗，不回傳 terminal success。實作需保存並復原被觸及的 registry memory；既有 _persist() 的 atomic rollback 不得被繞過。

### R8 — 測試契約

以 registry-only fixtures 測試，不呼叫 GitHub、provider、journal、authorization filesystem verifier 或 Manager finalizer。至少覆蓋：

1. 正例：完整合法 binding 將精確 ongoing verify/authority_restart run 原子轉為 ship/done，read-back 後所有既有 WorkflowRun 欄位／completion binding 符合預期。
2. 錯誤 phase/status/retry classification/run id/work id/repo/head、authorization hash 不一致、缺欄／錯格式／partial completion binding：拒絕且不寫。
3. 任一 dispatched/running active job 綁到同 run 時拒絕；無 active job 才可成功。
4. steps/gate refs 缺失或不完整、無效 gate kind、缺 required foreign-review/brainstorm/delivery review、多 delivery reviews、未通過 verify/review/ship step、builder/reviewer domain 重疊均拒絕。
5. trusted caller 重新驗證後，精確同值 done/ship re-entry 不呼叫 _persist 並保留原 updated_at；不同 terminal field、auth/record mismatch 或 active job 均拒絕且不覆寫。測試要明示：因無 origin marker，不能也不宣稱辨認相同 row 的 writer 來源。
6. 注入 _persist failure／atomic writer failure，並注入呼叫前 expected-state drift；重新讀 registry memory、run/job snapshots 與 raw durable state，確認沒有半套更新。
7. 使用一般 ongoing verify run、review/build run與非 authority_restart run，證明無法透過此方法越過一般 workflow gate；另確認一般 _manager_update_workflow_run() 與 phase validator 的原行為未改。

### R9 — 不新增外部 completion 工作

#976 不建立 CompletionRecord、不 append shipped outcome、不 dispatch/cancel jobs、不呼叫 proof oracle、不改 Manager completion order。成功後由 #977 Manager finalizer 負責既有 outcome-first、再呼叫此 API 的流程與 crash/retry recovery；#962 在所有 slices landed 後負責端到端 R8(a)–(d) 整合驗收。

## #887 parent acceptance ownership

| #887 AC | #976 負責範圍 | 非本票部分 |
|---|---|---|
| R1 | 不做 claim/admission routing | #975 admission、#977 finalizer |
| R2 | 僅提供受限 terminal primitive | #975 舊 run admission、#977 呼叫與 recovery |
| R3 | registry binding/precondition/active-job failure 不部分寫 | #975 proof fail-closed、#977 不產生 outcome/done |
| R4 | 無 journal helper | #975 phase-free helper |
| R5 | 不處理 authority source arbitration | 前置 #961 Child A |
| R6 | exact registry transition、record binding、same-value idempotence | #975 proof、#977 CompletionRecord/outcome/Manager decision |
| R7 | 不處理 source_revisions authority conflict | 前置 #961 Child A |
| R8(a) | 一般 review route仍不可走本專用 API | #975 admission、#977 end-to-end |
| R8(b) | registry 正例與相同 transition 重入 | #977 CompletionRecord、done/outcome整合 |
| R8(c) | registry active-job/field/head/auth/state-drift負例 | #975 proof負例、#977 無outcome/no-dispatch |
| R8(d) | 證明本 API 不讀 journal且缺 binding 不成功 | #975 journal admission、#977 fallback |

本表是責任切分，不修改 #887/#962 完整 AC。#962/#887 需待 #975、#976、#977 依序完成且其整合驗收通過後才可考慮關閉。

## 非目標

- 不實作或更改 #975 的 journal admission、trusted evidence、authorization/remote proof oracle；只消費既定 proof hash與validated CompletionRecord binding。
- 不實作或更改 #977 的 Manager completion orchestration、CompletionRecord建立、shipped outcome、outcome-first順序、done寫入流程或 crash/retry recovery。
- 不改 _manager_update_workflow_run()、validate_workflow_phase_transition() 或一般 verify/build/review gate規則。
- 不新增 WorkflowRun/Job JSON欄位、schema version、public API、CLI、provider、journal helper或 external evidence reader。
- 不取代 #962 全量 R8(a)–(d) integration tests；本票只交付 registry unit contract。
