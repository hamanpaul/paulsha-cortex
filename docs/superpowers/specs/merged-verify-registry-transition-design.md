---
status: accepted
work_item: merged-verify-registry-transition
---

# verify-reset merged run 的受限 registry terminal transition 設計（#976）

## Decisions

### D1 — production boundary is one private JobRegistry method

只在 paulsha_cortex/coordinator/registry.py 增加 _manager_complete_merged_verify_reset_workflow_run()。不改 manager.py、work_actions.py、workflow.py、general phase validator、job schema 或任何 external proof code。#975 負責 admission/proof；#977 的 Manager finalizer日後負責組出完整 binding、寫 CompletionRecord/outcome並呼叫此 method。

本方法是 registry-local gate：使用目前 JobRegistry 記憶體中的 run/jobs 做精確條件判斷，並將單一 WorkflowRun transition 一次 persist。它不是 GitHub/filesystem proof API，也不新增跨 process registry serialization/CAS；不得在本票順手擴成 #818/#966 的一般 registry writer contract。

### D2 — 固定輸入契約，auth hash 做雙來源比對

建議介面：

    _manager_complete_merged_verify_reset_workflow_run(
        run_id, *, expected_candidate_head, authorization_hash, terminal_binding
    ) -> WorkflowRun

terminal_binding 是固定 key set，包含 run_id、repo、work_id、candidate_head、authorization_hash、steps、gate_refs、completion_record_path、completion_record_hash、completion_record_revision、completion_source_revisions、pr_candidate、merge_revision。identity 與 completion data 由 Manager 從 validate_completion_record() 的正規化結果投影；authorization_hash 取該結果 work_authority.trusted_evidence_refs 唯一 merge_authorization entry 的 hash。#975 proof result 帶入獨立 authorization_hash argument。

#977 是唯一產品 caller；每次首次呼叫與重試前，它都須重新取得 #975 proof，並用 `completion.read_completion_record(path, expected_hash=writer_hash)` 讀回並驗證 record 及其 evidence refs。binding 的來源規則：slice_id/work_authority.run_id/repo/work_id/candidate、trusted merge-authorization hash 與 work_authority.merge_commit 取 normalized record；record path/hash 取可信 CompletionRecord writer result，並以 canonical normalized record hash 重驗；record_revision 與 pr_candidate 由 trusted closure metadata 綁定到同一 candidate SHA；merge_revision 同時比對 trusted remote-closure facts 與 record.merge_commit。`completion_source_revisions` 必須沿用目前 adapter 的精確投影：`{value.rsplit("@", 1)[0]: value.rsplit("@", 1)[1] for value in normalized["work_authority"]["source_revisions"] if "@" in value}`。重試時 current WorkAuthority 只供 proof/identity 驗證，不可重新計算或取代 record 的 frozen source map。

方法先驗證兩個 authorization hash 都是 64-hex 且完全相同，再驗證 binding identity 和目前 run 相等。registry 不確認檔案本身或 hash 是否真實；它只確保不同 authority source 的 hash 沒有在 Manager 邊界之間漂移。因 WorkflowRun schema 沒有獨立的 authorization_hash 欄位，不新增欄位；完成後由 completion_record_hash 承載對已驗證 CompletionRecord 全內容（含 trusted merge-authorization evidence ref）的 commitment。

binding 其餘 required values：candidate_head、completion_record_revision、pr_candidate 必須逐字等於 expected_candidate_head 且符合 SAFE_SHA_RE；merge_revision 是另一個合法 40-hex merge commit，不要求與 candidate 相等；completion_record_hash 是 64-hex；completion_record_path 非空；completion_source_revisions 是非空 string-to-string map。缺 key、錯型別、空值、不合法 digest 或多餘 key 一律拒絕，不以 current run 值補缺。

### D3 — 先完整驗證 registry-local state，再建構 terminal row

依序驗證：

1. exact run 存在，binding.run_id/repo/work_id/candidate_head 與當下 WorkflowRun identity 一致。
2. 若是新轉移，current run 必須 status=ongoing、current_phase=verify、retry_classification=authority_restart，且 current.candidate_head == expected_candidate_head。
3. self._jobs 中不得有 workflow_run_id 綁到此 run 且 status 在 ACTIVE_JOB_STATUSES（dispatched/running）的 job。
4. steps 是完整 tuple[WorkflowStep]，與目前 workflow manifest 保持相同 phase/card/persona spine；不得增刪、換卡或重綁 persona。只有完整 Manager proof 已授權的 state/evidence欄位可反映在 step values。gate_refs 是完整 tuple[GateEvidenceRef]，不接受任意型別或不合法 kind。
5. construct 後的 WorkflowRun 必須滿足現有 ship/done invariant：phase/status 與 gate passed、foreign-review、brainstorm-required（若適用）、恰一種 delivery review、verified head、verify/review/ship 全部 steps passed、builder/reviewer domain separation 及 completion fields 完整性。

檢查、構造與寫回都留在一個同步 method，不得呼叫 Manager callback、proof oracle、外部 I/O、await/yield 或 dispatch。active job 檢查與 registry row 變更之間不能回到事件迴圈接受另一個 generation。

### D4 — 僅對這一種舊 reset 狀態建立 verify→ship/done row

成功時以 dataclasses.replace(current, ...) 構造，不手動重新建立 run identity。只改下列既有欄位：

- current_phase="ship"、status="done"、verified_head=expected_candidate_head。
- steps、gate_refs、gate_status="passed"，全部來自 terminal_binding 的 Manager-verified values。
- completion_record_path/hash、completion_source_revisions、pr_candidate、merge_revision，依 D2 從 validated CompletionRecord 與 trusted writer/closure metadata 形成；completion_record_revision 必須等於 candidate SHA。
- facets=() 與 needs_human_reason=None，避免 done row 留有舊 needs-human診斷。
- updated_at=_now_iso()，只在首次 transition 的新 row 上蓋一次時間戳。

保留 candidate_head、retry_classification="authority_restart"、attempts、issue/pr/work identity、authority/source revisions與其他未列欄位。WorkflowRun constructor 的既有驗證照常執行；若沒有可證明的 gate/step，不可為了通過 constructor 自行標 passed。

方法不呼叫 validate_workflow_phase_transition(verify, ship)，因一般規則必須維持相鄰 phase 單調前進。以 method 的精確前置條件與 WorkflowRun 的既有終態 invariants構成唯一窄例外，不改一般 validator，也不允許 status=done 的通用更新由此方法流入。

### D5 — Exact terminal equality；不宣稱可辨認 writer origin

先處理既有終態：只有 current phase=ship/status=done、retry_classification=authority_restart，且 run/work/repo identity、candidate/verified head、auth argument與record投影 hash、所有 CompletionRecord fields、source-revision map、steps、gate_refs、gate_status、facets/reason都與本次完整輸入逐欄相等時，才回傳目前 row copy且不呼叫 _persist()。每次重入前，trusted #977 caller 必須重新驗證 #975 proof及 CompletionRecord reader/writer hash；API 自己只比對提供的兩個 auth hash及其餘欄位。已存 `updated_at` 是該 row 的既有 stamp，在此 no-op path 保留原值、不重寫。

schema沒有 transition-origin marker，ordinary writer 也可能產生完全相同的 persisted terminal values；因此本 API 無法辨認來源，不得宣稱該 row 先前由本 API 建立，也不得因缺少不可觀察的來源證明而拒絕完全同值重入。這個 no-op 只確認「目前值等於本次 trusted Manager 再驗證後要求的 terminal projection」，不證明歷史 writer。任何 terminal field、auth/record 對應或 active-job 狀態不同均拒絕、不覆寫；不一致的 done/ship run不會被修補。

### D6 — Memory/durable rollback

所有型別、binding、precondition 與 WorkflowRun constructor checks 都在變更 self._workflows 前完成。保留被替換的 workflow row/list snapshot；將 updated row 暫存並呼叫既有 _persist()。若 _persist() raise，restore 該 row/list 到呼叫前狀態再 re-raise；不回傳成功。既有 _persist()/atomic writer 必須負責 durable rollback，任何 atomic write failure後讀回 raw jobs.json、get_workflow_run() 與 list_jobs() 都與呼叫前 snapshot一致。首次轉移使用此新 row 的 updated_at；exact no-op re-entry 不呼叫 _persist 且保留原 timestamp。

測試分兩種 fault：在 transition API 的 _persist 邊界注入 pre-write failure，證明 method 自己回復 memory且 durable bytes未改；再透過既有 atomic writer seam 注入落盤故障，證明 state檔 rollback 與 run/job snapshots 均一致。Expected state drift在所有 mutation 前拒絕，不呼叫 persist。

### D7 — 不把 proof/Manager completion 混入 registry

API 只接收 immutable/typed terminal_binding 值，不能讀 delivery-journal.json、authorization path、PR provider或 CompletionRecord path；不能從 record path自行重建 binding。它不判斷 proof是否trusted、不建立record/outcome、不寫任何 other state。單元測試只用固定的 validated-binding fixture，零 GitHub network。

#977 finalizer日後應先拿到 #975 proof，再 validate/produce CompletionRecord binding，先寫唯一 shipped outcome，然後呼叫本API。該順序及 crash/retry 是 #977/#962 的責任，不在此實作或由 registry 模擬。

### D8 — Test design 與現有 source oracle

新增 tests/test_registry_merged_verify_transition_976.py，透過 JobRegistry 既有 create/get/list methods建立 run、steps、gates與 job snapshots。使用 monkeypatch/spies只控制 registry persist failure；proof、CompletionRecord/terminal binding 測試資料明確固定，不呼叫網路/Manager。

正例檢查 `WorkflowRun.from_dict` restart/readback後 status、phase、verified head、gate refs、steps、CompletionRecord path/hash/revision/source revisions、PR candidate及merge revision完全符合輸入；assert _persist恰一次且首次 transition stamp 變更。Same-value retry 的測試 fixture 可由任一 writer 建立同值 done/ship row；在 caller 提供重新驗證過的 proof/record-binding 值後，assert _persist呼叫數為零且 updated_at 保留原值。測試明示不能從現有 row 推斷 writer origin，不製造 provenance assertion。所有 rejection都檢查 raw file bytes、run與job snapshots未變。

現有 source 已提供必要 invariant：JobRegistry._manager_update_workflow_run() 先呼叫 validate_workflow_phase_transition()，該 validator只接受同 phase或恰好下一 phase，故 verify→ship現行拒絕；WorkflowRun.__post_init__ 已驗 done completion fields與ship gate/step/domain invariants；ACTIVE_JOB_STATUSES 正是 dispatched/running。本票只加窄 API與專屬測試，不改上述既有 contract。

### D9 — 五維 sizing（accepted triad 後實算）

- domain_breadth=0：唯一 production module為 coordinator/registry.py。
- state_consistency=2：同一 durable registry row 上要求 active-job/run generation gate、single transition atomicity、persist-failure rollback、完整 terminal field equality及保留 first-transition timestamp 的精確 no-write idempotency；現有 schema 無 origin marker，規格不加 provenance 推斷。
- acceptance_surfaces=2：正式 helper 的 process-level rules為 R-09/R-16/R-19；fix-standard 有 2 gate_spine，signal=2+3=5，得 2。
- spec_stability=0：三份 accepted planning artifact完整且無 blocker，依 stability-risk-v2。
- orchestration=2：fix-standard cards=9，persona_bindings=9。

實際預期總分 6/Yellow。若 planning artifacts未 accepted，spec_stability風險為2，暫得8/Red，不得派工。正式 implementation intake前再用當時 linked combo與 current_sizing_snapshot()重算；若 production scope擴出 registry.py、需新增schema，或 proof/finalizer code被混入，重算並重新拆票。

### D10 — Dependency gate

先合併 #961，完成 #975 admission/proof oracle並凍結其 run/head/authorization-hash binding。#976 implementation與整合驗收等待 #975 合併；API contract對 #975 的 proof output只取 hash/identity mapping，不接管proof。#977 僅在 #975與#976都完成後接手Manager completion orchestration。#962需在三者 landed後跑 R8(a)–(d) integration；#887最後再核全部parent AC。

## 邊界摘要

| Owner | 責任 |
|---|---|
| #975 | run admission、journal/authorization/trusted evidence/remote merge proof oracle |
| #976 | registry.py 私有 restricted transition、typed/full-binding與run/job preconditions、原子persist/idempotency/rollback unit contract |
| #977 | Manager CompletionRecord、shipped outcome、finalizer ordering與crash/retry integration |
| #962/#887 | 全體切片後驗收R8(a)–(d)及parent完整AC，不被單一child完成關閉 |
