---
status: accepted
work_item: slice-recovery-registry-identity
issue: 968
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Slice recovery registry identity 設計

## Decisions

### D1 — 名稱與 identity 分開

slice_id 維持人讀名稱。每筆新 slice row 都取得由 registry 配發的唯一、不透明 slice_identity；不得從顯示名稱、spec path、branch、job ID 或 repo 目錄產生。repository_identity、owner_work_key、worktree_identity 是獨立欄位，不塞進或重用 slice_id。

Identity-aware create 接受明確的 repository/owner/attempt 資料。Work lane 的 repository 是已驗證 WorkAuthority.repo，owner 是呼叫端用既有 work_key(repo, work_id) 算出的原字串。Registry 只做型別、格式、唯一性與 row/job 一致性檢查，不認證呼叫端的 authority。owner key 可空；帶 owner 的 row 必須有非空 repo。缺 repository／attempt provenance 的相容舊 API row可以讀寫其他既有用途，但標記為 unbound，禁止作為 #547 recovery target。

### D2 — 建議的 additive row shape

Slice row 增加 top-level 欄位：

- slice_identity：registry 配發的不透明、非空、不可變 ID。
- repository_identity：Work Item lane 的 canonical WorkAuthority.repo；沒有明確 repo provenance 時只能是 null/unbound。
- owner_work_key：精確 work_key(repo, work_id) 字串或 null。
- worktree_identity：opaque attempt ID；尚未配置 attempt 時可為 null。

所有新建 row 都保存上述四個欄位；非 recovery-capable 的 generic row可用明確 null 表示尚無 owner/repository/attempt provenance，但不能被 recovery resolver 使用。Repository/owner/attempt 欄位一旦形成有效 binding，不可由一般 update 改寫。尚未配置 attempt 時 worktree_identity=null；在 workspace 建立前，producer 必須以明確新 attempt ID 設定為非空值。

帶 identity 的 job row 增加 repository_identity、slice_identity、worktree_identity 三欄。create_job 只有在三欄全缺（legacy/unbound job）或全有且格式有效時接受；部分欄位拒絕。Identity-bearing job 必須先能透過 slice_identity 找到 slice row，且其 repo 與當前 attempt identity 必須逐字相等。Job 保存建立時 identity；後續 slice repin 不回寫歷史 job。

`create_job` 的 active-builder 守衛也按 durable identity 分流：identity-bearing builder 只與同一 `(repository_identity, slice_identity)` 的 active builder 衝突，不能只因 task 或顯示名稱相同就跨 repo／Work Item 阻擋；同一 row 的 active builder 即使 `task` 不同仍衝突。identity-less legacy 呼叫保留舊 task-scoped 守衛，以免改變既有相容路徑。Identity-bearing 呼叫不得把部分 tuple 降級成 task-only。不同 `slice_identity` 的同名 row 不互擋；同一 tuple 的不同 attempt 不得並行。

不新增 jobs.json root 欄位或第二個檔案。新欄位沿用 JobRegistry 現行單一 JSON snapshot 與 _persist()；不另造 journal、receipt、lock 或 rollback。

### D3 — Attempt 與 repin

Attempt identity 不等於 physical path。Producer 在建立 clone/worktree、job workspace 或其他目錄前先以 registry API 落盤。Slice row 保存 current attempt 的 worktree_identity；job row 複製建立時的三欄 identity。builder_job_id 可清除、repin 或更換，均不影響 slice/attempt identity。

Identity-aware repin 必須按 slice_identity 定址，帶入全新的 worktree_identity；同一次現有 registry persist 更新 slice current attempt 並清除既有 mutable job pointers。slice_identity、repository_identity 與 owner_work_key 不變；已建立的舊 job 仍保留原 attempt ID。只清除或更換 builder_job_id、但未開始新 attempt 時，slice 的 worktree_identity 不變；repin 明確開始新 attempt 時才換新 ID。重用其他 row/job 中已保存的 worktree identity 必須拒絕。若 slice 已採 #862 versioned binding，這項 repin 必須走 #862 原 writer 並在同次 persist 遞增 binding_revision，不能繞過其 CAS 世代。工作區建立與 marker 寫入由 #969 負責；本票僅建立可供該 producer 使用的持久欄位與 API。

### D4 — Label API 僅在唯一時相容

現行 create_slice 不再以全 registry 唯一的 slice_id 當 row primary key；具有不同 identity 的重複顯示名稱可共存。新增 exact API（名稱可依 repo 命名慣例微調，但語意不可變）：

- get_slice_by_identity(slice_identity)
- update_slice_by_identity(slice_identity, ...)
- repin_slice_by_identity(slice_identity, ..., worktree_identity=...)

所有既有以 slice_id 呼叫的 get_slice、update_slice、repin_slice、record_action 等路徑共用 fail-closed resolver：0 筆為 not found，1 筆維持舊行為，多筆丟出明確 AmbiguousSliceError；不得回第一筆、取最舊／最新或依 state 排序。讀取 owner key 時保留完整候選，不加唯一約束；候選排序只可供診斷呈現，不能決定選取。update_slice(None) 相容語意不變。

### D5 — Loader 與 copy 相容

- 舊 slice row 的四個 identity 欄位全缺，視為 legacy/unbound，可讀而不改寫原 row 或檔案 bytes；read copy 可增加衍生欄位 identity_binding_status="legacy_unbound"，但不可放回內部 row。
- 新 row 四個欄位齊全；null 僅依 D2 規則表示明確 unbound／尚未配置 attempt。缺少其中一個 key、部分 owner tuple、格式錯誤、重用唯一 ID 或 row/job 不一致皆 fail closed，不降格成 legacy。
- 所有 copy/reload API 保留 identity 值；nested copy 規則沿用現行 JSON copy 慣例。一般 update 不接受 identity 欄位，防止 caller 透過展開舊 row 回寫 owner。
- Load/read/update 不用 slice_id、spec path/basename、branch、repo 內路徑、job worktree path 或 builder_job_id 自動補任何 identity。

### D6 — #862 原語與 legacy checkpoint 整合

#862 仍是唯一的 registry revision、receipt、CAS、checkpoint 與 durable transaction owner。#968 只新增 identity 欄位、row/job 一致性與 exact-ID registry API；不修改 recovery/checkpoint payload、binding revision 或 proof contract，也不建立第二 writer。identity-aware repin 若作用於 #862 versioned row，必須沿用該票接受的 writer/revision 規則；legacy row 不因一般操作自動升級。

#862 recovery target public v1 payload 目前只含 `repo/work_id/slice_id`，且現行 `_find_slice(slice_id)` 是 first-match。不能把 external prefilter + first-match CAS 當 exact target；但同交易 identity pin、commit-time reselect、legacy checkpoint record/persistence 與 workspace proof verifier 均屬 proposed AC7 child-chain integration，#968 不實作或測成已通過。AC7 child chain (issue numbers TBD) 必須在 registry transaction 內重選並 revalidate persistent `slice_identity`，cardinality 非 1、identity drift 或 pin 缺漏即零變更 fail-closed。對尚未有 slice identity 的 legacy row，AC7 child chain (issue numbers TBD) 使用完整 observed-row fingerprint 與 target 做同交易唯一選取，並把 migration record/row identities/checkpoint receipt 同次落盤。

依 owner 核定的 A1/AC7 拆分，#968 對應 A1（AC1–AC6 與 active-builder guard）；AC7 child chain (issue numbers TBD) handles the existing live #968 migration-record requirement under the proposed #547 aggregate AC7。這份 triad 是規劃拆分草稿，不表示 live #968/#547 已更新。即使 #968 A1 完成，#547 proposed aggregate AC7（新增提案） 仍 OPEN，直到 AC7 child chain (issue numbers TBD) 同時通過 durable persistence 與真實 proof verifier 驗收；不得以 A1 完工關閉 live #968 migration-record clause 或 #547 proposed aggregate AC7。

### D7 — Registry-focused failure matrix

專用 test module 建議為 tests/test_slice_recovery_registry_identity_968.py，使用每例獨立 tmp registry。至少包含：

- 同名跨 repo、同名跨 Work Item、相同 owner key 多筆、同 owner 同 label 多筆；assert 所有候選仍保留，不能 first-match。
- 舊 slice_id 唯一 API 正常、多筆 API 逐個讀／改皆 ambiguous；按 slice_identity 的 API 只改 exact row。
- 新 slice identity 唯一、repository/owner tuple 格式與 immutability、identity-bearing create_job exact tuple；錯 repo、錯 slice、錯 attempt、缺一欄、ID collision 均拒絕。
- active builder 守衛正反例：identity-bearing job 同一 `(repository_identity, slice_identity)` 即使 task 不同仍阻擋；只有各自有效且 durable tuple 不同的 row（包括同名但屬不同 repo 或不同 Work Item 且各有自己的 `slice_identity`）才可同時建立；identity-less legacy call 保留 task-only 行為；部分 identity tuple fail closed。
- 在 create workspace 的模擬前 durable reload attempt ID；單純清除或更換 builder_job_id 不清同一 attempt ID；明確 repin 才接受未使用的新 ID，舊 job 保留舊 ID。
- general update、deep copy、fresh registry reload、repin 後 repo/slice/owner/attempt 的預期差異精確；update 不可寫 identity 欄位或更換 owner。
- legacy row全缺欄位可 read as legacy_unbound；read/update 不增加 key、不改 durable bytes；任何 partial/malformed shape fail closed。
- 不測或宣稱 AC7 migration record/proof verifier 已完成；這是 AC7 child chain (issue numbers TBD) 的 hard acceptance gate。#968 測試只需確保 legacy row 維持 `legacy_unbound`、exact identity APIs 不把它選成 recovery-capable row。

預期使用 RED/GREEN 對照證明每個必要負例；通過後才跑 registry 直接回歸與 repo 必要 gates。所有測試維持 fixture 隔離，不藉 live state、model session、workspace deletion 或名稱相似度製造證據。

## Scope and acceptance mapping

| #968 AC | Design | Planned proof |
|---|---|---|
| AC1: same label, distinct durable owner/identity | D1, D2, D4 | duplicate-label tests, exact-row read and no foreign mutation |
| AC2: attempt identity before workspace, independent of job pointer | D2, D3 | persist/reload before simulated workspace; clear/repin job pointer |
| AC3: matching job/slice repo/slice/attempt tuple; identity-aware active-builder isolation | D2, D3, D7 | valid and one-field-at-a-time mismatch cases; same durable slice blocks, same name/task in foreign repo or different slice identity does not |
| AC4: identity tuple correct through reload/repin/update; owner immutable | D3–D5 | fresh registry and ordinary update preserve tuple; explicit repin changes only current attempt ID, and old job retains prior ID |
| AC5: legacy read-only unbound; no name migration | D5 | byte-stable load/read/update plus name-collision fixture |
| AC6: permit duplicate owner rows; resolver ambiguity | D4, D7 | preserve all rows and reject every ambiguous selector |
| New proposed parent aggregate AC7 (separate from #968 A1) | deferred to AC7 child chain (issue numbers TBD) | AC7 child chain (issue numbers TBD) must atomically persist the migration record, row identities, and #862 checkpoint receipt, after a named verifier validates real workspace identity; #547 remains open until then |

## Five-dimension sizing

使用 repo 現行 planning.compute_sizing_score 尺度；分數描述此 work item 的規劃範圍，不代表執行資格或 dispatch readiness。

- **domain_breadth = 0**：唯一 production module 是 paulsha_cortex/coordinator/registry.py。Tests、此三件文件與 delivery documentation 不另算 production domain；若 implementation 觸及 autonomy/manager/work_actions/dispatcher，必須重算並重劃 scope。
- **state_consistency = 1**：#968 持久化 slice/job identity tuple、exact identity APIs 與 legacy reload/no-writeback；資料仍由 registry 單檔 `_persist()` 更新，沒有新增 legacy checkpoint/receipt/CAS transaction。AC7 的高一致性 durable migration 已移給 AC7 child chain (issue numbers TBD)，不計進此 A1 sizing。
- **acceptance_surfaces = 2**：fix-standard 有 2 個核心 gate-spine entries。此 API/code＋tests 需 R-09 changelog 與 R-19 tests；不改 CLI/help，所以 task-applicable rules 為 {R-09,R-19}，signal=2+2=4，依 0／1–2／>2 門檻得 2。若 generic caller 傳入全體 {R-09,R-16,R-19}，signal=5 結果仍是 2。
- **spec_stability = 0**：此 triad 是依 root 已裁決的 A1-only scope 定稿，design 無未決契約。Live #968 body 尚待同步；文件另列為 pre-dispatch issue-alignment gate，不把尚未更新 live body 冒充設計不確定性。
- **orchestration = 2**：current fix-standard 有 9 cards、9 張含 persona binding（>1），依現行函式得 2。
- **總分 5 / Yellow**：0 + 1 + 2 + 0 + 2 = 5。此為已拆分後 A1-only triad 的 sizing；實際 helper 對本 triad 回報 `complete=True`、五維 `0/1/2/0/2`。Live #968 仍包含 migration-record acceptance bullet，故 issue owner 必須先採用同目錄 `issue-body.md` 的 scope/dependency 對齊；這個 A1 分數不代表 live issue 已更新、#547 proposed aggregate AC7 已完成，或本票已可派工。

## Source basis and limits

規劃依 live #968、#547、#862、#969–#971 及目前 checkout source 核對。當前 registry evidence：

- paulsha_cortex/coordinator/registry.py：`_persist` line 617；`_find_slice` lines 1011–1016 依 slice_id 回第一筆；`create_job` lines 1119–1125 active-builder guard 只比 task/persona/status；`create_slice` line 1470 拒絕任何重複 label；repin/get/update 等仍以 label 定位。當前沒有 durable identity fields 或 exact-ID guard。
- paulsha_cortex/coordinator/job_workspace.py：`read_marker`/`write_marker` lines 490–520；現 marker shape 只有 schema_version/model/branch/base/source_repo/created_at，沒有 #547 repo/slice/attempt identity，故不能當 AC7 verified proof。
- Live #862 I09/D7：checkpoint 單 transaction 寫 binding_version/revision=1 與 checkpoint receipt、保留舊 business fields；proof refs 由外部 caller 驗真，A 只驗 shape/digest，不打開 refs。其 payload 目前沒有 #968 identity record 或 `slice_identity` target pin。
- Live #969：marker/producer 身份由 B 建立，但 issue 明列不做 legacy auto-migration；live #970/#971 只在 A–C 完成後作 recovery/core/resolver，尚無 legacy proof verifier owner。
- paulsha_cortex/monitor/work_snapshot.py:20：work_key(repo, work_id) 是現存 canonical repo::work_id helper。
- paulsha_cortex/deck/data/combos/fix-standard.yaml：9 cards 與 2 core gate-spine entries；9 referenced cards 都有 persona binding。
- paulsha_cortex/coordinator/planning.py:766-770, 901-920：stability-risk-v2 與 acceptance surface 三規則白名單；`fix-standard` 2 gate-spine + {R-09,R-19} 得 acceptance=2，9 cards/9 persona binding 得 orchestration=2；有 blocker 的完整 triad stability-risk=2。
- .project-policy.yml：tests gate 與 policy R-09/R-19；此票不改 CLI/help，故 R-16 不適用。

以上證據意味#547 proposed aggregate AC7（新增提案） 不能以目前 #862 API 完成；該硬閘現由 AC7 child chain (issue numbers TBD) 承接，要求 #862 owner 在同一 transaction 支援 durable migration record/row identities，並指定能驗真 workspace proof 的 owner。這些是 AC7 child chain (issue numbers TBD) 的 blocking dependencies，不是已完成 integration。本 triad 只覆蓋 #968 A1 的 AC1–AC6 與 active-builder isolation；它不表示 live issue 已更新。這些是 source/planning evidence，不是 implementation 或 test evidence。本次沒有新增產品程式、沒有實跑產品測試或 Cortex workflow，亦未建立/合併/發布 OpenSpec、PR、changelog 或 source binding。


## Pre-dispatch issue-alignment gate

This design is scoped to the six newly numbered A1 acceptance items plus the identity-aware active-builder guard. Live #968 still contains a final migration-record acceptance bullet; the issue owner must adopt the A1-only replacement and dependency graph before dispatch. The new proposed aggregate AC7 remains separate under #547 and is not live numbered acceptance today.
