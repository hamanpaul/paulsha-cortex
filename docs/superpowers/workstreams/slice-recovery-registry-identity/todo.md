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

# Slice recovery registry identity 工作計畫

## Boundary

- 唯一 owner [#968](https://github.com/hamanpaul/paulsha-cortex/issues/968)，正式 work_item 為 slice-recovery-registry-identity；父項 #547 第 2 點；若引用 AC 編號，使用待採用的 proposed aggregate AC3。依 owner 核定拆分，本 triad 的 #968 範圍只含 AC1–AC6 與 identity-aware active-builder guard；live #968 migration-record clause 移到新子票 AC7 child chain (issue numbers TBD)，仍是新 proposed aggregate AC7 的 hard gate（待 parent issue owner 採用）。這是草稿規劃，不代表 live issue 已更新。
- 規範與決策見 [spec](../../specs/slice-recovery-registry-identity-spec.md) 及 [design](../../specs/slice-recovery-registry-identity-design.md)。
- Product production scope 限 paulsha_cortex/coordinator/registry.py；新增 focused registry tests、OpenSpec、runbook/docs 與 changelog 屬交付所需文件。若為滿足本票而需改 autonomy、manager、work_actions、CLI 或 workspace/reclaim production module，先停下重定義 issue scope與 sizing。
- row identity 只建立精確定位所需的資料契約；不把 A 的完成當成 #547 recovery resolver、reclaim 或雙入口 parity 已完成。
- #547 第 3 點已裁決：retire-delivered 維持由 cortex work gc proposal-first 回收，不新增即時自動 worktree 清理。本票不改該政策。
- 本 todo 全部未勾表示尚無產品實作、測試或交付證據。Accepted planning 不代表 Cortex claim/run、PR、merge、installed 或 live 驗收。

## Dependency and ordering

1. #968（A1）交付 registry identity row/job schema、exact ID APIs、fail-closed label ambiguity 與同名 active-builder isolation；root 需更新 live #969 dependency 以讓 B 在 A1 後開始。
2. #969 blocked by #968/A1：Producer 取得 WorkAuthority/Work Item provenance，並在建立 workspace 前寫入 attempt identity 與 marker tuple。
3. AC7 child chain (issue numbers TBD), blocked by #862、#968/A1、#969：完成live #968 migration-record clause 的 same-CAS durable identity migration record 與真實 workspace proof verifier。
4. #970 blocked by #968/A1、#969：兩個 recover-pre-candidate 入口共用狀態機與 handoff supersession；未遷移 legacy rows 必須保持 unbound，不能當 candidate。
5. #971 blocked by #968/A1、#969、#970：Work lane 用完整 authority 唯一解析 slice owner。
6. #547 仍 OPEN，直到 A–D 實作驗收（對應 live narrative points 1–2）、新增 proposed aggregate AC7 child chain (issue numbers TBD) 通過，且父票 owner 接受的 aggregate criteria 整合驗收完成；A1 完成不關 #547。

[#862](https://github.com/hamanpaul/paulsha-cortex/issues/862) 也修改 registry.py，且目前仍 OPEN。其 accepted contract 擁有 binding revision、receipt、CAS、checkpoint 與 atomic transaction。#968 在實作前須重讀 live #862/實際 merge 狀態，序列整合同一 module；重用其適用 writer/revision，不複製 transaction/receipt，不以舊 checkout覆蓋另一票。Legacy identity migration、同交易 exact target reselect、durable record/receipt 與 workspace proof verifier 均已移到 AC7 child chain (issue numbers TBD)；#968 不驗或宣稱#547 proposed aggregate AC7（新增提案） 完成。AC7 child chain (issue numbers TBD) 的硬依賴為 #862 owner 接受同 CAS transaction contract 並指派 proof-verifier owner。

## Tasks

- [ ] **T01 source／I01／I06**：在 registry slice row 增加持久 slice_identity、repository_identity、owner_work_key、worktree_identity 欄位。Registry 配發唯一 slice_identity；identity-aware row 僅接受明確 repository/owner/attempt inputs，沒有 provenance 的相容通用 row 保持 unbound。不要由顯示名稱、spec/branch/path 或 job ID 合成欄位。
- [ ] **T02 source／I03／I04／I05**：新增以 slice_identity 為 key 的 get/update/repin API，保存所有同名 slice row與相同 owner_work_key 的多筆 row。既有所有 slice_id-only lookup/mutator 在唯一時維持行為，多筆時明確 ambiguous；包含 record_action 等內部呼叫路徑，不得 first-match 或排序選取。保留 update_slice(None) 既有 no-op 語意。
- [ ] **T03 source／I02／I03／I04**：identity-bearing job row 同存 repository_identity、slice_identity、worktree_identity；create/update 驗證三欄完整、可找到 exact slice_identity row，並與 row 當前 repo/attempt 相等。`create_job` active-builder guard 對 identity-bearing jobs 僅以 `(repository_identity, slice_identity)` 判斷同一邏輯 row：同 row 任務名稱不同仍阻擋；不同 repo、Work Item 或 slice identity 即使 task/slice_id 同名也不互擋。identity-less legacy 呼叫維持舊 task-only 守衛；部分 identity tuple 拒絕，不降級。只清除或更換 builder_job_id 但未開始新 attempt 時，worktree_identity 保持；Identity-aware repin 明確開始新 attempt 時接受新的唯一 identity，保留 repo/slice/owner 及歷史 job identity，並在 workspace 建立前同次落盤。若是 #862 versioned row，repin 必須沿用其 writer/revision。
- [ ] **T04 source／I03／I06**：更新 loader、copy、read、update、repin 與 JSON reload 保留欄位並嚴格拒 partial/malformed shape。Legacy row 全缺欄位仍可 read as legacy_unbound；不 backfill、不做 load/read/update writeback。只在 read projection 標記 unbound，不將衍生標籤存回 durable row。
- [ ] **T05 tests／I01–I06**：新增 `tests/test_slice_recovery_registry_identity_968.py` 隔離測試；逐項對照 #968 AC1–AC6 和附加 active-builder guard。覆蓋同名跨 repo/Work Item、同 owner 多筆、label API ambiguous、exact slice_identity 操作、job/slice 三欄錯置、active-builder foreign same-name allow/same durable row block、workspace 前 reload、清除／更換 builder_job_id、repin、一般 update、fresh reload、legacy byte-stable no-writeback、partial/malformed identity。不得讀 live jobs.json 或建立／刪除 live workspace。AC7 proof/migration 不在此票測成通過，由 AC7 child chain (issue numbers TBD) 覆蓋。
- [ ] **T06 tests／regression**：執行 identity focused tests、registry/headless slice lane 直接回歸，再依 repo preflight 跑適用 gate；完整 tests 按 .project-policy.yml 使用 python3 -m pytest tests/ -q。對任何實際失敗先分類，不能用 unrelated skip 或 fixture 假成功。R-19 由 tests workflow 涵蓋；本票不改 CLI，R-16 help sync 不適用。
- [ ] **T07 docs／OpenSpec／changelog**：在正式 product run 建立此 work_item 自己的 OpenSpec proposal/design/spec/tasks，與 accepted triad、五維 0/1/2/0/2、invariant_count=6、AC/test matrix 同步。新增 changelog.d/slice-recovery-registry-identity.md 並更新 CHANGELOG.md [Unreleased]；更新 registry/recovery contract docs，明列 legacy unbound、same-name ambiguity、#862 boundary、AC7 child chain (issue numbers TBD) handoff 與 #547 parent gate。不要把 planning 文件或 prototype 當產品測試證據。
- [ ] **T08 review／delivery evidence**：PR body 清楚分帳 focused/full tests、policy、獨立 review、remote CI、exact-head merge，以及任何未跑 gate。變更若擴到第二 production module、加入新的 durable transaction/CAS、或無法滿足 AC1–AC6 與 active-builder isolation，先重估 sizing/issue scope。完成 #968 A1 不關#547 proposed aggregate AC7（新增提案）。

## Acceptance mapping

| Replacement #968 AC | Todo tasks |
|---|---|
| AC1: 同名 rows 有獨立 repo/owner/slice identity，不覆蓋 | T01、T02、T05 |
| AC2: worktree identity 在 workspace 前 durable，與 builder_job_id 解耦 | T03、T05 |
| AC3: job/slice 的 repo/slice/attempt identity 一致，錯置拒絕；同名 foreign active builders 不互阻 | T03、T05 |
| AC4: reload/repin/update 維持正確 identity transition，owner 不可改寫 | T02–T04、T05 |
| AC5: legacy/unbound 可讀，不按名稱遷移 | T04、T05 |
| AC6: 相同 owner 多筆可存、ambiguous 不可選一筆 | T02、T05 |
| Live #968 migration-record clause / new #547 proposed aggregate AC7: durable legacy migration record and verified workspace proof | moved to AC7 child chain (issue numbers TBD); keeps the proposed aggregate AC7 open on #547 until the child chain (issue numbers TBD) passes. AC7 child chain (issue numbers TBD) blocked by #862 contract/owner, #968 identity APIs, and #969 marker schema. |

## Five-dimension sizing

- **domain_breadth 0** — 僅 registry.py production module；若實際需要改 producer/consumer/path reclaim，停止並重劃 child。
- **state_consistency 1** — #968 persists slice/job identity tuples and strict legacy/reload/API semantics in the existing registry snapshot; it does not add a migration CAS/receipt/transaction. AC7's cross-row/receipt transaction moved to AC7 child chain (issue numbers TBD).
- **acceptance_surfaces 2** — fix-standard 有 2 個 gate-spine entries。此 source/test change 適用 R-09 changelog 與 R-19 tests，不改 CLI/help所以排除 R-16；signal 2+2=4，得 2。若 planner caller 注入全三規則，2+3 仍得 2。
- **spec_stability 0** — A1 spec/design/todo define the root-approved AC1–AC6 plus active-builder guard scope without unresolved design questions. Live #968 body alignment remains a pre-dispatch intake gate and is not evidence that live #968 is already ready.
- **orchestration 2** — current fix-standard 為 9 cards/9 persona bindings，超過一個，得 2。
- **總分 5／Yellow** — 0+1+2+0+2. This is the post-scope-alignment score for #968 A1. The live issue still needs the issue-owner body/dependency update before intake; this score does not pass parent AC7, the AC7 child chain, implementation, tests, or Cortex run.

## Evidence and limitations

規劃依 live #968/#547/#862–#971 與目前 source/planning 核對。`registry.py:create_job` active guard 只比 task/persona/status，`_find_slice(slice_id)` 為 first-match；#968 A1 以 durable IDs 修正 row/job selection 與 same-name active-builder isolation。live migration-record acceptance 移到 AC7 child chain (issue numbers TBD)，不能由 A1 完成替代。此資料夾只有規劃 triad；目前沒有 #968 source/tests、PR/checks、changelog、正式 OpenSpec 或 merge/runtime evidence。issue owner 尚需更新 live #968/#547/#969 dependency，否則目前 live blockers 未改。

## Split handoff

Live #968 migration-record clause is not implemented by A1. The proposed AC7 child chain consists of #862 contract acceptance, Builder proof helper, fixed separate-UID runner, operator auth, explicit action caller, same-CAS writer extension, and #970 markerless recovery adapter. All child numbers are TBD; see the child README for exact dependencies. The issue owner owns publishing the children and aligning live #968/#547/#969 dependencies.
