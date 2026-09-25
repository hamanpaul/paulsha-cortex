---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
---

# Monitor trusted freshness 設計

## Context

`WorkModelRefresher.refresh()` 目前在一次 refresh 中掃描 provider、建立 correlation/projection 並寫入 `WorkSnapshotStore`。`correlate_work_sources()` 透過 `load_work_item_overrides()` 讀取 `.cortex/work-items.yaml`，但 correlation 沒有回傳實際 parsed bytes 的 revision。Snapshot store 會持久化 sequence、providers、sources、rows 和 ownership；sequence 不能證明最新 attempt 成功。provider degraded 時可保留 last-good source，matching row 因而不能作 freshness proof。

本 change 只處理 #1078 的 successful evidence publication 與 read-only API。#1077 管 generation allocation、running/failure marker storage；#1063 管 source qualification/path contract。若 #1077 未提供 expected-generation success transition，先完成依賴 contract，不能在此 change 新做 marker writer。

## Goals / Non-Goals

**Goals:** capture exact input bytes/revision; retain same-generation provider/source revisions; verify durable snapshot read-back before success; expose typed fail-closed repo/work freshness.

**Non-Goals:** generation allocator、running/failure writer、Todo qualification/path admission、WorkAuthority consumer、Manager gate、CLI、recovery、ship、intake 或 deployment。

## Decisions

### D1 — One raw read feeds both parser and revision

Refresh 只讀一次 override bytes；以 `sha256:<lowercase-hex>` 形成 revision，然後把同一 bytes 傳給 YAML parser。明確 missing file 使用 `absent:v1`；permission/read/decode/parse errors 為 failed outcome，不得成功。freshness API 以相同方式讀取目前 input 後比較 revision，不接受 caller hash。

### D2 — Per-repo manifest comes from this candidate only

manifest 記錄 correlation input revision、該 repo 使用的 provider ID/revision，以及 candidate 中實際消費的完整 `source_id → revision`。source rows 必須來自同一份即將 durable-write 的 WorkSnapshot candidate。provider/source 缺證、unknown、degraded、stale、generation 不符或從 last-good fallback 保留時，不得發布 success。

### D3 — Persist and verify WorkSnapshot before success

流程先寫 candidate snapshot，從 canonical store path reload，核對 canonical digest、sequence、目標 repo/work rows、source ownership、provider revisions 與 source revisions。所有 read-back 欄位吻合後，才透過 #1077 提供的 expected-generation 操作將同一 running generation 轉為 succeeded 並存放 manifest/snapshot evidence。該操作必須拒絕 generation 已變更或狀態非 running 的更新。中間 crash 保持 running/failed，舊成功不能回退為最新成功。

### D4 — Resolve root and age bounds inside Monitor

唯一 public API 接受 repo/work ID。它由 Monitor 最新 `ProjectState` 集合取得 refresh 使用的 canonical repo root；無 root 或有歧義時回 untrusted。讀取 latest per-repo attempt、current input、success manifest 與 durable snapshot，核對同代 identity、digest/sequence、target ownership、provider/source revisions 和 `stale_after_seconds`。API 不觸發 refresh、寫 marker、補 legacy evidence 或修改 snapshot。

### D5 — Stable typed failure results

API 回傳 immutable typed trust status、stable reason code、generation 與僅經驗證的 input/source/snapshot evidence。reason 至少區分 missing/legacy/unknown/running/failed marker、malformed data、ambiguous/missing root、input mismatch、source/provider mismatch、generation mismatch、snapshot missing/drift、stale 和 I/O error；同時多個錯誤採固定優先序。任何缺證都不能以 last-good rows、sequence、空 refresh error 或 in-memory candidate 取代。

### D6 — Preserve snapshot compatibility

WorkSnapshot 保持既有 `work-items-snapshot/v1` schema。success manifest 儲存在 #1077 定義的 generation marker contract；舊 snapshot 沒有同代 marker/read-back evidence 時仍可供 listing/diagnostic，但 freshness 一律 untrusted，不做 backfill。

### D7 — Keep the ordered ownership chain

#1078 必須 consume #1077 generation/store contract；不得 allocate generation 或複製 durable writer。它依賴 #1063 facts，不重做 qualification/path guard。#1064 umbrella 僅在 #1077 和 #1078 都完成後收尾；#1065 與 #1054 分別保持 consumer 與 gate owner。

## Failure behavior

Snapshot write/read-back mismatch、manifest construction failure 或 expected-generation transition failure 不得產生 trusted result。latest marker 由 #1077 按既有 failure semantics 保持 running/failed；snapshot 即使已寫入也只供診斷。Freshness query 只讀最新 marker，不回退舊成功。

## Open Questions

無。#1077 的 same-generation success publication contract 是本票實作前置條件，失配時先對齊依賴合約。
