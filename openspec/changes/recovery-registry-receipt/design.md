---
status: accepted
work_item: recovery-registry-receipt
issue: 862
---

# Recovery registry receipt 設計

## Decisions

### D1 Ownership 與交付順序

規劃骨架為 `A → C → B → D2`，另 `D1 → D2`。A 只改 `coordinator/registry.py`；C 是 work_actions，B 是 manager（含 work shim），D1 是 control/contract 加 coordinator/cli 與 porcelain/recover，D2 是 manager_daemon。A 可先提供局部原語，不能宣稱公開 recovery、complete_tick 或父 S03 已修。root 2026-09-08 的工程裁決已納入 D7/D8，並已接受八件內容；唯一 child owner 為 [#862](https://github.com/hamanpaul/paulsha-cortex/issues/862)，`work_id: recovery-registry-receipt`。status=accepted 不等於 dispatch：規劃 PR 尚未合併發布/binding、T10 證據仍缺，現行 accepted8 Red；#831 的6 Yellow仍僅投影。

`repo/work_id` 是 B/C 以既有 WorkAuthority 驗過後注入的 provenance；registry 本身沒有該 mapping 的權威來源。A 可核對 exact slice_id、row/job refs、snapshot、digest 與 replay 一致性，**不能憑 caller 的 repo 字串認證 repository ownership**。actor/requested_by 是 audit labels，不是新授權憑證。

OpenSpec 文件生命週期不是第二個 product module：自身 `openspec/changes/recovery-registry-receipt/{proposal.md,design.md,specs/**,tasks.md}` 的唯一 owner/source authority 是本 child。**產品 run start 前**由正式 repository-intake planning 責任方建立、審完、合併/發布、strict validate、唯一 mapping/source binding/read-back，固定 exact refs/revisions；不依賴產品 run 的 propose 階段，完整 accepted triad 可能直接 plan gate→build。Own proposal/design 必須 self-contained 且與本 child 三件組等價，specs delta 一致；own tasks.md 的0/2/9、artifact classes、全部 Tasks/surfaces/rules 與 Todo 一致，使 first-plan/last-sizing 選取不分歧。缺件/矛盾回正式 intake，不在 frozen run 補造 authority。Candidate 對這些 frozen planning artifacts 只准既有 checkbox toggle，operator/intake baseline immutable。T10 只驗既存前置證據及 archive 前文件；archive 只處理自身 change，不封存 umbrella；產品 merge/installed/live、C/B/D 與母收尾留 prose。Root 已確認八件 fresh review PASS 並接受內容，唯一 child owner 為 #862；本輪只更新接受狀態，未規劃 PR 合併發布/binding/archive，T10 前置證據仍缺，不能派工。

### D2 Proposed v1 request 與 digest

Recovery 內部 context 的固定欄位為 `version`（精確 `cortex/recovery-registry-request/v1`）、`request_id`、`payload`、`request_digest`；不接受缺欄位或未知欄位。`request_id` 是 D 從原 control req_id 原封不動轉入的非空字串；單一 registry 的所有 recovery/checkpoint receipt 共用唯一 ID 規則（D7）。A 不產生 request ID，也不把重送轉成新 intent。

`payload` 固定為 `{request_type, action, target, expected_binding, required_steps, actor, requested_by, created_at}`。request_type 為原 control 的 slice-action/work-action；本 recovery transaction 的 action 固定 recover-pre-candidate。target 為 `{repo, work_id, slice_id}`，slice 入口無 canonical work_id 時須明給 null，而不是虛構 mapping。expected_binding 欄位見 D3。required_steps 是 B/C 依已核可 recovery 契約選定、D 在送出 immutable context 前綁定的非空 step ID array；A 不選政策。step ID 須符合 ASCII `[a-z][a-z0-9_.:-]{0,127}`，列表按 ASCII 升冪且無重複；不同集合必須得到不同 digest，未排序/重複輸入拒絕而非任意 normalization。所有 mutation-affecting 輸入都必須列在這個 versioned payload；D 不得排除一個有副作用的 extra 參數後仍稱 digest 相同。

`request_digest = sha256(prefix || J({version,request_id,payload}))` 的 lowercase 64 hex；prefix 是 UTF-8 `cortex/recovery-registry-request/v1` 加單一 LF。J 是 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 的 UTF-8 bytes，無 BOM/尾端 LF，不做 Unicode normalization。字串須能嚴格 UTF-8 編碼；本 shape 除 binding_revision 外只准 string/null/object，以及 required_steps 所在位置的 string array，不接受 float、bool、NaN/Inf、surrogate 或未知 node type。revision 為 1..9007199254740991 的 JSON integer，bool 不算 integer。versioned shape 拒絕 extra/duplicate JSON keys；duplicate text key 偵測屬 D ingress，native mapping 到 A 時已不能還原 duplicate。A 重新計算 digest，不信任 caller 填的值；D1/D2 必須消費同一 encoding，不能各自換 serializer。

目前 `verification.canonical_json_hash` 可供編碼語意參照，A 的實作不得為此修改第二 module 或放寬該既有通用 helper。實作時需固定完整 request golden bytes/hash，並測鍵序重排等價、actor/target/revision/created_at 改變不等價、ID 相同內容不同衝突。這是尚未實作的驗收要求，不以虛構 golden hash 冒充證據。

### D3 Binding snapshot 與 ABA

expected_binding 固定包含 `{binding_version,binding_revision,builder_job_id,reviewer_job_id,candidate,state,gate_state,spec,plan,verification_hash,target_branch,target_remote,dispatch_base}`；binding_version 精確為 `cortex/slice-binding/v1`，spec/plan 各為 `{path,hash}`。完整提供，null 與 omitted 不等價；值與 live row 精確比較，不 trim 或以目前值補缺。此 recovery 要求 candidate=null、state 為 needs_human/failed，且 builder/reviewer refs（非 null 者）存在；合法 SHA dirty case 不得走此操作。A 的結構/CAS 驗證不替代 B/C 的 allowed-action/permission gate。

fresh create_slice 建立 revision=1；已 versioned row 的 update_slice/record_action 改變此 snapshot 任一業務值時，revision 在同一次 persist 遞增；每個成功 repin 即使值相同亦遞增，明確 recovery commit 亦遞增。只改 updated_at、追加純歷史說明、lookup 或 exact replay 不遞增。達上限時拒絕、不 wrap/reset；新的 epoch/version 需後續正式契約。

候選 mutation seams：registry.py 的 create_slice:1395、repin_slice:1446、update_slice:1500、record_action:1591，以及本 child 新增操作。這是當下靜態佐證，不是「不存在其他 writer」的全稱證明：需 table-driven public-mutator 行為測試及 frozen snapshot/ABA 回歸；發現漏掉的 registry writer 必須納入，同步重評，第二 module 則停交 root。

舊 API 參數與 `update_slice(None)` 語意保留；query 不 backfill。root 已裁決：缺 revision 的既存 row 由舊 mutator 相容繼續，但維持 unversioned，不自動由 repin/update/record_action 升級、不宣稱 ABA 保證。只有 D7 明示 checkpoint 或 fresh create_slice 建立新 versioned 起點；後者不是舊 row migration。已 versioned row 即使透過既有 API 改動 binding，也須依本節遞增；這不等於舊 unpinned public recovery 已安全，C/B/D 仍須接線。

### D4 Additive row shape 與 operation phases

只增加 slice row 的 `binding_version`、`binding_revision`、`recovery_receipts`、`binding_checkpoint_receipt` 與 job row 的 `supersession`／`consumption`；不新增 jobs.json root fields。binding_version/revision 同時缺失且無 checkpoint receipt 才可視為 legacy；單獨缺欄位、帶 checkpoint receipt 卻無 version/revision、或非空 recovery_receipts 卻缺 version/revision 均 malformed，不藉刪欄位重置世代。Fresh create_slice 可有 version/revision 而無 checkpoint receipt；不能反推曾做 legacy migration。Recovery receipt、supersession、consumption 的 version 分別精確為 `cortex/recovery-registry-receipt/v1`、`cortex/job-supersession/v1`、`cortex/job-consumption/v1`；checkpoint receipt 見 D7。absence 為 legacy/未記錄，未知或 malformed version 為拒絕，不降格成 absence。

receipt 固定記錄 `{version,request_id,request_digest,payload,phase,prepared_at,completed_at,applied_binding,affected_job_ids,required_steps,step_receipts,result}`。prepared 時 completed_at/applied_binding/result 均 null，step_receipts 為空；complete 時全部必需資料齊全。receipt.required_steps 必須逐值等於已納入 request_digest 的 payload.required_steps，不接受 prepare/commit 另帶不同政策清單。step_receipts 每項含 `{step_id,ref,sha256}`，ref 為非空字串、sha256 為 lowercase 64 hex；step IDs 不重複、集合須恰等於 required_steps。同 ID 降低/替換 proof requirements 即 conflict，不能聲稱 request 不變；即使初次 prepare 也不可產生同 digest 但不同 requirements 的 receipt。準備者及 proof verifier 的 authority 屬 B/C；A 檢查 schema/digest/集合閉合，不打開 proof ref、不呼叫任何檔案/網路 producer。假的 fixture receipt 只測 A 結構條件，不冒充真 proof 或真人批准。

proposed operations：`lookup_registry_request_receipt`（read-only、搜尋全 slice 的兩類 receipt，D7）、`prepare_recovery`（重新 CAS 後只記 prepared）、`commit_pre_candidate_recovery`（再次 CAS 與 closure check，原子完成業務更新）。名稱尚未實作，不對 CLI 暴露。prepared 重送若原 binding 已變，保留原 receipt、回 stale；不能用 prepared 放行新的 intent。完整 receipt 在 binding 已換代後仍可回原 immutable result，但不再操作 slice；相同 ID 不同種類或 digest 永遠 conflict。

不存在跨檔「一次原子完成」：B/C 先 owner-bound reclaim/驗 proof，再要求 commit。若資源工作部分成功而 CAS 失敗，A 的 prepared 不升 complete、業務 row 不變；B/C 負責 exact old resource reconciliation，不能把資源已刪除的事實回滾承諾塞入 A。prepared 不是鎖，也不替 #818 解多 writer；B/C 必須在資源副作用前後驗同一 owner/binding。

### D5 Atomic commit 與 disposition

成功 commit 在一個 staged registry snapshot 中：revalidate request/digest/revision/完整 tuple 與 refs → 為舊 builder/reviewer 記 supersession → slice/gate 設 pending、三個 binding/candidate 欄位明確 null → revision+1 → 追加一次 action/history → 固化 complete receipt → 沿現有 `_persist` 單次落盤。prepared 是先前獨立成功的 intent 寫入，不是本次失敗 rollback 要抹除的既存狀態。

job supersession 至少有 `{version,job_id,slice_id,binding_revision,actor,at,reason,superseding_identity}`；identity 是 exact 新 binding identity 或 recovery request_id/digest。consumption 至少有 `{version,job_id,slice_id,binding_revision,actor,at,completion_identity,proof_refs}`；proof refs 為帶 ref/hash 的已驗證閉合證據。這兩欄不互推、不因 terminal exit 就生成，不覆蓋已完整歷史。A 提供 CAS/冪等儲存；B/C 仍擁有真 replacement/consumption call sites，A 不在舊 mutator 中猜 actor/request receipt 或把所有 rebind 都自動宣告 superseded。

版本正確但 incomplete proof、identity 相衝或 refs 指錯 job 必須拒絕；同一已完成 disposition exact replay 不追加。已 consumed 後又被合法新 attempt 取代可另記 supersession，但不能抹掉先前 consumption。拒絕不是刪除舊證據、改 evidence 位址或放寬 immutable writer 的理由。

### D6 Failure、相容與 proof limits

重用 `_write_payload_atomically:572–615`／`_persist:617–641`，不重做 #821。fault fixtures 驗 staged validation、temp write、rename 前後、directory fsync、rollback restore：rollback 成功時 memory/file 都等於呼叫前；若 rollback 本身失敗，回明確 fatal persistence error，不宣稱成功或舊 bytes 已恢復。此既有不可恢復分支需留可診斷證據，不能以忽略 exception 假綠。

最低錯誤語意：`unsupported-recovery-version`、`malformed-recovery-context`、`recovery-binding-required`、`legacy-binding-unversioned`、`request-content-conflict`、`stale-binding`、`incomplete-recovery-proof`、`recovery-persistence-failed`。A 可以 typed exception/result 表達；D 的 CLI/queue/done 對應不在 A。未知 version 不以零/預設 schema 續跑。

現有 loader 的 v1 migration 與 #501 repair、deep copy、optional additive fields 必須保留既有 fixtures 與行為，不刪除或改為新授權入口；不能宣稱 byte-preservation 普遍成立。對 A 新欄位本身，load/query/reload 應無寫入且不插入 default；若本來會走既有 migration/repair，允許原有寫入，但不得順便生成 A version/revision/receipt。新 record 的 nested copy/restart 驗證屬本 child。

### D7 Explicit legacy checkpoint primitive（root OQ01 裁決）

新增 registry-only proposed `checkpoint_legacy_binding`；只能由未來 C/B 的正式 owner-bound action 明示呼叫，A 不認證 actor 字串、不發明 public CLI 動詞或權限。它不呼叫 recovery，不要求 candidate=null 或替 C/B 放行某個 state；若 C/B 合法接受的 row 有既存 candidate SHA，checkpoint 也只能原樣保存，不能清它。Target 是既有 loader/migration 政策處理後，live v2 slices 中仍無 A version/revision 的 row；`legacy_records` 內被隔離的 v1 audit row 不可藉此復活為 live slice。

Checkpoint context 固定 `{version,request_id,payload,request_digest}`，version 精確 `cortex/legacy-binding-checkpoint-request/v1`；不能冒充 D2 recovery context，也不接受 binding_revision=0/1/default 作為 expected legacy pin。payload 固定 `{operation,target,expected_legacy_row,expected_legacy_binding,expected_job_refs,legacy_snapshot_fingerprint,actor,requested_by,created_at,provenance}`；operation 精確 `checkpoint-legacy-binding`，target 與 D2 相同。expected_legacy_row 是 caller 在明示觀測時取得的完整 slice row，包含所有當時欄位、nested values/history/metadata/未知保留欄位，不剔除 updated_at 或 evidence；沒有 A version/revision/checkpoint receipt。expected_legacy_binding 是 D3 snapshot 去除 binding_version/revision 後的完整欄位，必須與 supplied row 的同名/projection 資料一致；expected_job_refs 是 `{builder_job_id,reviewer_job_id}`，含明確 null，必須同時等於 supplied binding 與 row。A 不從目前 row 補 caller 缺的 snapshot/pins。供此觀測使用的 get/list row、傳入 context 的 staged copy 與回傳 receipt 都須 deep copy 完整 nested 值（包括未知保留欄位），不能讓 caller alias 改動 registry 內部快照；這是 I07 copy 契約的 checkpoint 延伸，不是新增 writer。

Fingerprint envelope 固定 `{version:"cortex/legacy-binding-snapshot/v1",slice:expected_legacy_row,binding:expected_legacy_binding,job_refs:expected_job_refs}`；`legacy_snapshot_fingerprint=sha256(UTF8("cortex/legacy-binding-snapshot/v1\n") || J(envelope))` 的 lowercase 64 hex。J 沿 D2 的精確 Python canonical JSON encoding，但 checkpoint 的完整 legacy snapshot 允許 JSON null/bool/string/object/array/integer/有限 binary64 float；不得 trim、轉型或省略未知欄位。Int 1、float 1.0、bool true、string "1" 不可混同，-0.0 與 0.0 亦依 J bytes 區分；非 finite／surrogate／非字串 key／循環 native object 拒絕 checkpoint，但不授權重寫既有 state。JSON text 的數字由 caller 的 JSON decoder 保留為原生 int/float（無小數/指數為 int，其餘為有限 binary64），不以整數化修復 float。A 自行重算 supplied fingerprint，並對 freshly revalidated live snapshot 重算，**以 canonical bytes 與 fingerprint 比對，不以 Python dict 的 true==1 等寬鬆 equality 代替**。這是觀測資料的 canonical 身分，不是原 state 檔排版的 hash。

Checkpoint request_digest 為 `sha256(UTF8("cortex/legacy-binding-checkpoint-request/v1\n") || J({version,request_id,payload}))`；完整 snapshot/fingerprint、refs、provenance 全在 digest 內。provenance 固定 `{owner_action_ref,observed_at,authority_ref,proof_refs}`；前三欄非空字串，proof_refs 是非空 `{ref,sha256}` 列表（ref 非空、sha256 lowercase64hex、ref 不重複）。它們是 C/B 必須驗真的外部權威／觀測證據之不可變引用，A 只驗結構與一致性，不讀取 ref、不把有字串當批准。A 不回填或自造 provenance、觀測時間與 request ID。

Transaction 次序：驗 context/version/digest → 在全 registry 所有 slices 的 recovery_receipts 與 binding_checkpoint_receipt 查 request_id → exact 同種類/digest complete replay 回原 receipt，零 mutation；不同種類/digest/target conflict → 對新 ID revalidate 仍 legacy、完整 row/binding/fingerprint 未漂移、非 null job refs 均存在且等於 current binding → staged 新增 binding_version=`cortex/slice-binding/v1`、binding_revision=1、不可變 checkpoint receipt → 單次 `_persist`。原 row 的所有既存欄位（含 updated_at/actions/history/candidate）原樣保存；不清 binding、不標 disposition、不改 job。Job refs 的存在/身分核對不證明其 worktree/owner/proof 真偽，C/B 在呼叫前重驗這些資料；checkpoint 本身無資源副作用。

checkpoint receipt 固定 `{version,request_id,request_digest,payload,phase,checkpointed_at,applied_binding,provenance,result}`，version=`cortex/legacy-binding-checkpoint-receipt/v1`，phase 只允許 complete，result 精確 `checkpoint-created`；applied_binding 是原 expected binding 加上述 version/revision1，provenance 逐值等於 payload.provenance。它沒有 prepared/recovery-success/consumed 語意。validation/persist failure 不能留下有 revision1 卻無 receipt 的 row；rollback 比照 D6，並不保證 rollback 本身失敗時仍可恢復。Crash 後載到完整 checkpoint，exact 同 ID replay 回原 receipt；若 caller 改用新 ID 重做，因 row 已 versioned 拒絕 `legacy-checkpoint-not-applicable`。即使後來 row 已到 revision N，同 ID replay 也只回歷史結果，不退回1。

Global ID 規則涵蓋上述兩類 A receipt 的全部 slices/狀態，不只目標 slice 或 pending/active；load 發現相同 ID 多份或跨種類衝突 fail-closed，不任選一份或新增 root index 欄位。Checkpoint 與隨後 recovery 是兩個不同 intent，必須各自有 immutable request ID；A 不自動衍生第二個 ID、不連帶 recover。舊 receipt 保留，不能 prune 後重用 ID。

**歷史殘餘**：沒有 legacy revision 時，觀測前或兩次觀測間若歷史曾 A→B→A 且完整資料也恢復相同，fingerprint 不能追證；本交易只證明本次明示觀測與 commit 當下相符，從 revision1 往後開始計次。receipt 必須保留這個 provenance/界線，不能聲稱修復歷史 ABA 或補齊過去 audit。所有新 versioned writer 按 D3 計次；既有 unpinned public action 的整體安全仍依 C/B/D 正式接線，不由 A checkpoint 局部成功替代母 S03。

### D8 最小 read/reload 相容範圍（root OQ02 裁決）

A additive 欄位缺失本身不得觸發 `_load` 中的 normalization/persist 或 default backfill。現有 v1 migration／#501 repair 原政策、原 fixtures 不改；它們不是本母項新增欄位承諾中的全面零寫入範圍，亦不在此建立替代 migration 動詞。測試要分開證明「不需既有 migration 的 v2 row bytes 不變」與「本來會 migrate/repair 的 fixture 保持原結果且不添加 A 欄位」，不能把後者當零寫入 PASS。

## Verification

隔離 tmp_path registry fixtures；不讀 live jobs.json，不啟 manager、不呼叫模型。至少覆蓋：fresh v2、缺 A 欄位 v2、v1、#501 repair row、未知 version、明確 null/缺 pins、same/different ID+digest、跨 slice 同 ID、stale revision、A→B→A、identical repin、prepared restart、complete restart、兩舊 jobs 一次 supersession、既存 consumption 保留、mutation copy、commit failure、rollback failure。新增 checkpoint 正例（含 candidate SHA 原樣保存）、完整 snapshot/metadata/nested drift、fingerprint 偽造、job refs 漂移/缺失、漏 provenance、version0/未知version、同 ID recovery↔checkpoint 衝突、跨 slice/狀態 ID 重用、首次 transaction write fault/rollback、restart 後 exact replay、revision N 不倒退、legacy 舊 mutator 不自動升級、新 versioned writer 正常計次。I01–I08 原案例全部保留。

每個成功/拒絕案例同時驗 memory、durable bytes、revision、history/receipt count；不要只看 return code。prepared/complete fixture 的外部 proof 用 immutable 假資料只驗 schema，不宣稱 B/C/D 路徑、reclaim、manifest、10 ticks 或 public allowed-action 已通過。

## Closed Engineering Decisions

- **OQ01 closed 2026-09-08**：採 D7 顯式新 checkpoint，沒有歷史 ABA 追證／legacy grandfather。C/B 的 owner-bound action/proof、D 的原始 identity 傳遞另有 owner，不算 A 已完成。
- **OQ02 closed 2026-09-08**：採 D8 最小相容範圍，不動既有 v1/#501 loader policy。全面 loader 改造不在 A。

這是 root 已接受的 #862 repository planning intake，不是 actor/真人資格授權、產品實作或 live checkpoint。九組 invariants、state=2 不降；status=accepted 只接受內容，規劃 PR 合併發布/binding、T10 與 dispatch gates 尚未完成。
