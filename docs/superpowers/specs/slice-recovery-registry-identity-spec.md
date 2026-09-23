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

# Slice recovery registry identity 規格

## Authority 與範圍

唯一 issue authority 是 [#968](https://github.com/hamanpaul/paulsha-cortex/issues/968)，父項為 [#547](https://github.com/hamanpaul/paulsha-cortex/issues/547) 第 2 點；「AC3」只指待 issue owner 採用的 parent aggregate acceptance proposal。依 owner 核定的拆分，這份 triad 將 #968 限為 AC1–AC6：registry slice row/job 的 durable repository、Work Item、slice、attempt identity、API、驗證與同名 active-builder 隔離；legacy migration record 與 proof 驗證移到新子票 AC7 child chain (issue numbers TBD)。此 triad 草稿尚不代表 live #968/#547 已更新；在 issue owner 更新 live acceptance/dependency 前，live #968 migration-record clause 仍在 body，須由 issue owner 接受 A1-only replacement 後移出本票。

[#478](https://github.com/hamanpaul/paulsha-cortex/issues/478) comment 2 記錄 `builder_job_id` 清除後無法定位原工作區。#547 要求以 durable identity 取代名稱、路徑及 job pointer 啟發式。`slice_id` 是顯示名稱，不是 repo 或 Work Item 的 ownership key。

本規格 status=accepted 代表規劃內容已定版；不代表產品程式、測試、Cortex run、PR、merge、安裝或 live recovery 已完成。#968 不關閉 #547 或 #862。

## Identity contract

- `slice_identity`：registry 內一筆 slice row 的不透明、不可變、唯一 ID，由 registry 配發；不從 `slice_id`、路徑、branch 或 job ID 產生。
- `repository_identity`：Work lane 由已驗證 WorkAuthority 提供的 canonical `repo` 字串。registry 可驗證型別與欄位一致性，不能自行驗證 WorkAuthority 或授權來源。
- `owner_work_key`：可為 null；有 Work Item owner 時必須逐字等於既有 `work_key(repo, work_id)` 的結果。Registry 不得從 `slice_id` 或 basename 推導它。
- `worktree_identity`：不透明的 per-attempt identity，不是 worktree 路徑，也不是 `builder_job_id`。新的 attempt 必須在建立 workspace 前由 producer 提供並先寫入 registry。
- `slice_id`：保留供 operator 顯示與既有入口使用；重複名稱合法，名稱不參與 identity 比對。
- 對沒有明確 repository／attempt provenance 的通用或舊資料，identity 狀態為 unbound；這類 row 不得成為 #547 Work lane 的候選。不得為了讓它可選而合成 repo、owner 或 attempt identity。

## Requirements

1. **I01 — Row identity 與同名隔離**：identity-aware 新 slice row 保存 `slice_identity`、非空 `repository_identity`、可空 `owner_work_key` 與當前 `worktree_identity`（尚未配置 attempt 時可為 null）。相同 `slice_id` 可對應不同 row；不同 repo 或 Work Item 的 row 各自保留不同 durable identity，不覆寫、不合併、不互相取得 owner。每個新 row 都有獨立 `slice_identity`。
2. **I02 — Attempt identity**：identity-aware row 的每個 attempt 使用新的 `worktree_identity`。Producer 先把該值寫入 row，再建立 workspace；repin 以新 attempt identity 更新該 row 的 current identity，不能清除或以 `builder_job_id` 取代。舊 job record 保留它建立時的 identity。
3. **I03 — Slice/job 一致性與 active-builder 守衛**：帶 identity 的 job record 保存與其 slice row 相同的 `repository_identity`、`slice_identity`、`worktree_identity`。若 repo、slice 或 attempt 任一不符，create/update API 必須拒絕；identity-bearing job 必須先能指向其 exact `slice_identity` row。Identity-bearing `create_job` 的 active-builder 守衛以 durable `(repository_identity, slice_identity)` 定位同一邏輯 slice：同 row 上任何 attempt 的 active builder 都拒絕；只有各自有效且 durable tuple 不同的 slice row 才可不互相阻擋（包括同名但屬不同 repo 或不同 Work Item 且各有自己的 `slice_identity`）。Identity-less legacy `create_job` 保留既有 task-scoped 守衛；部分 identity tuple 拒絕，不降級成 task-only。一般 copy/read/update 與 fresh JSON reload 完整保留 identity；明確 repin 只能把 row 的 current `worktree_identity` 換成 caller 提供的新 attempt ID，slice/repo/owner 不變，舊 job 保留舊 attempt ID。
4. **I04 — Immutable owner 與 exact row API**：`slice_identity`、`repository_identity` 與已設定的 `owner_work_key` 不得被一般更新、同名 row、新 job 或重試改寫。僅 attempt transition 可用新值更新 `worktree_identity`。新增按 `slice_identity` 定址的讀取／更新／repin API；既有按 `slice_id` 的 API 在 label 唯一時維持相容，遇多筆時明確報 ambiguous，不得回傳或修改第一筆。
5. **I05 — 多筆與歧義保真**：相同 `owner_work_key` 可保存多筆 row；registry 不對 owner key 加唯一限制、不以 state/candidate/job pointer/排序來挑一筆。Registry 暴露完整候選或由 caller 用 exact identity 定址；後續 resolver 遇多筆必須回 ambiguous。
6. **I06 — Legacy 明確 unbound**：舊 row 缺 identity 欄位仍可讀，但 read surface 明確標為 `legacy_unbound`，不在 load、read、一般 update 或 copy 時推導、回填或寫回 identity。若 identity 欄位只出現一部分或格式不合法，load/API fail closed；不得把 malformed 當 legacy。`builder_job_id`、spec path/basename、branch、repo 內路徑及 `slice_id` 都不是遷移來源。

## 驗收條件與 coverage

以下每一項都必須有 focused registry test；測試使用隔離的暫存 `jobs.json`，不讀 live registry、不啟動 Manager 或 workspace reclaim。

| Replacement #968 acceptance | 規格 | 測試 oracle |
|---|---|---|
| 相同 `slice_id` 的 row 有不同 `slice_identity`、repo、owner；跨 repo/Work Item 不覆蓋或取得相同 owner | I01、I04、I05 | 建兩筆同名 identity-aware row；分別讀回各自欄位。按舊 label API 讀／改必須 ambiguous，按 identity API 只影響指定 row |
| workspace 建立前已持久化 attempt identity；builder job 清除或 repin 後仍讀得原 attempt identity | I02 | 先寫入並 reload；清空或更換 `builder_job_id` 後 identity 不變；repin 僅切換到新的 attempt identity |
| job 與 slice 三個 identity 相等；錯 repo/slice/attempt 拒絕 | I03 | 正確 tuple 成功，三個欄位各自錯置均拒絕且 memory/durable row 不變 |
| 同名跨 repo／Work Item 的 active builder 不會互相阻擋；同一 durable slice 的 active builder 仍拒絕重複派工 | I03 | 建兩筆 `task`／`slice_id` 相同但 `(repository_identity, slice_identity)` 不同的 active builders，可各自建立；同一 tuple 不論 task 是否同名均拒絕；identity-less legacy calls 保留舊 task-scoped 結果；部分 tuple 拒絕 |
| JSON reload、repin、一般 update 後 identity 保持正確；同名 Work Item 不改寫 owner | I03、I04 | fresh registry 與一般 update 保留 tuple；repin 僅換成明確新 attempt ID、slice/repo/owner 不變且舊 job 留舊 ID；另一 owner 的 row 不改變 |
| 舊 row 可讀並明確 unbound；任何正常路徑不做名稱遷移 | I06 | legacy fixture 缺所有 identity keys，read projection 標 `legacy_unbound`；比較 load/read/update 前後 durable bytes，沒有新增 identity 欄位 |
| 相同 owner key 可保存多筆；resolver 不可由 registry 順序選擇 | I05 | registry 接受兩筆相同 key；列表保留兩筆；任何 label-only lookup 及後續 owner resolution 都回 ambiguous |
| 新增的 #547 proposed aggregate AC7：legacy migration record 與真實 workspace proof | moved to AC7 child chain (issue numbers TBD); remains a parent hard gate until AC7 child chain (issue numbers TBD) passes durable same-transaction persistence and named proof-verifier acceptance |

## 非目標

- 不解析或驗證 WorkAuthority；producer 必須傳入由已驗證 WorkAuthority 得出的 `repository_identity` 與 `work_key(repo, work_id)`。
- 不修改 spec frontmatter、workspace marker、worktree 建立／刪除或 reclaim；#969 負責 producer，#970/#971 負責 recovery core 與 Work lane resolver。
- 不在本票實作 recover-pre-candidate admission、state transition、handoff manifest supersession、`next_actions` 或 terminal job supersession。
- 不定義或持久化 legacy migration record，不執行 workspace proof 驗證；這些由 AC7 child chain (issue numbers TBD) 在 #862 checkpoint 與 #969 marker contract 上承接。#968 只讓所有缺 identity 的 row 繼續 unbound，不推導或回填。
- 不變更 `update_slice(None)` 語意，不由 `builder_job_id` 或同名標籤回推歷史 attempt。
- 不改 #547 第 3 點政策；`retire-delivered` 仍由 `cortex work gc` proposal-first 回收，不新增即時自動 worktree 清理。


## Pre-dispatch issue-alignment gate

This draft defines the A1 replacement scope. Live #968 currently includes a final legacy migration-record requirement; the proposed replacement removes that requirement from A1 and leaves the new parent aggregate AC7 as a separate proposal under #547. The issue owner must adopt the body/dependency alignment before dispatch. Live #547 currently has no numbered AC.

## Five-dimension sizing

依現行 `compute_sizing_score` 與 `fix-standard` 實算：domain_breadth=0（單一 production module `registry.py`）、state_consistency=1（單檔 registry snapshot，無 AC7 transaction）、acceptance_surfaces=2（2 gate-spine + R-09/R-19，signal 4）、spec_stability=0（按本 draft 已定版的 A1-only scope 計分；live body synchronization is a pre-dispatch gate, not unresolved design）、orchestration=2（9 cards/9 persona bindings）。**A1 scope-aligned score 5 / Yellow.** 此為 live issue body 對齊本 triad 後的五維分數，不表示目前 live #968 已對齊或可派工；issue owner 完成 pre-dispatch body edit 後才能 intake。此分數不代表 AC7 或 #547 完成。
