---
status: draft
work_item: monitor-correlation-refresh-generation
issue: 1064
---

# Monitor correlation refresh generation 設計

## Context

此design與`docs/superpowers/specs/monitor-correlation-refresh-generation-design.md`是同一issue/work_item的平行view。現行`WorkSnapshotStore` durable寫入整份last-good payload；`WorkModelRefresher.refresh()`會先從上一份snapshot承接provider state，呼叫`correlate_work_sources()`讀`.cortex/work-items.yaml`後投影WorkItem；provider degraded時可能保留last-good source。`ProjectMonitorService._refresh_work_model()`只在exception時呼叫`record_refresh_failure()`，其錯誤由`WorkReadModelStore`記憶體持有；這些資料不能證明latest durable correlation是否成功或涵蓋目前override。

Parent owner為live issue 1064。producer slices依賴次序為#1063 qualification/path admission → #1077 durable attempt ledger → #1078 source/snapshot binding與freshness API → #1064 umbrella completion → #1065 WorkAuthority consumer → #1054 Manager gate。source qualification屬#1063；consumer、gate、recovery與ship均不在此change。

## Goals / Non-Goals

**Goals:** persistent monotonic attempts；failure marker與last-good payload分離；成功record綁定實際override/source revisions及durable snapshot read-back；提供唯一read-only repo/work trusted freshness API。

**Non-Goals:**改claim authority rules、Manager dispatch/claim gate、Todo qualification、path guard/override mutation、pre-Candidate或Candidate/PR recovery、ship semantics、CLI command、live intake、implementation/deployment。

### D0 — Producer split and dependency contract

#1077 only publishes durable generation/running/failure state. #1078 depends on that marker contract and adds exact consumed revisions, read-back verification, success publication, and the trusted freshness API. #1064 is the umbrella and is complete only after both slices; no consumer or Manager gate is moved into either producer issue.

## Decisions

### D1 — Per-instance sidecar marker

在snapshot旁用獨立versioned marker file保存`generation`、`running|succeeded|failed`、timestamps及per-repo outcome。從strictly validated durable current marker遞增generation，先atomic-write `running`再掃描。unknown/malformed marker不重設generation；API直接untrusted。單一refresher lock負責同instance內序列化；若實際部署有跨程序共寫，必須加入durable CAS/file lock或拒絕第二writer。

### D2 — Bind marker to exact inputs

correlation parser回傳同一次raw-byte read所產生的`.cortex/work-items.yaml` SHA-256；缺檔採固定absent revision。attempt record保存本repo source providers及`source_id → revision`，所有資料來自同一份即將寫入的WorkSnapshot candidate。marker把input/source manifest與durable snapshot digest/sequence綁為同一generation；WorkSnapshot不必增加新field。不得把last-good rows當本次成功輸入；任一required provider/correlation result degraded即repo outcome failed。

### D3 — Snapshot-first, marker-last commit

先持久化running marker；成功candidate以現有WorkSnapshotStore atomic write保存後，由durable path reload並比對canonical digest、sequence、target rows、provider/source revisions。只有read-back證明candidate完整且相同，才原子寫succeeded marker，綁定generation與snapshot sequence/digest。任何crash或write failure使marker保留running／failed；快照仍可留作診斷，不構成trusted freshness。

### D4 — One trusted freshness API

在`WorkModelRefresher`提供唯一`trusted_correlation_freshness(repo, work_id)`等價entry。refresher從最近一次service傳入的canonical ProjectState集合記錄unique repo-root mapping；API內部用該root讀目前override、marker與snapshot，caller不能給路徑/hash/boolean。缺少或不唯一root時untrusted。嚴格核對per-repo latest attempt success、generation、snapshot digest/sequence、current input revision、source revision set、provider freshness與configured `stale_after_seconds`。回傳typed trust/reason/evidence；不得從matching row、sequence或空`last_refresh_error`推論fresh。

### D5 — Keep current snapshot compatibility

marker sidecar不改`work-items-snapshot/v1` row schema。舊snapshot仍供既有listing/debug讀取，API因缺marker而fail closed；不自動backfill。#1065將來消費本API，但不在此接線。

## Risks / Trade-offs

- **[snapshot與marker是兩次durable write]** → running先寫、snapshot read-back後成功marker最後寫；中間故障只會降低可用性，不會放行舊success。
- **[marker損壞停用trusted API]** → 明確reason與診斷，保留last-good rows但不猜測或自動修復generation。
- **[repo root mapping可能不存在於冷啟動時]** → resolver未知即回untrusted；等Monitor建立canonical ProjectState再查。
- **[current override在掃描後變更]** → API read-back current raw revision時不符即untrusted，直到下次完整correlation success。

## Open Questions

無。若code trace發現marker path可能有多process writer，依D1採durable coordination並重新sizing，不降低monotonicity。
