---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
---

# Monitor trusted freshness 設計

## Context

目前 `WorkModelRefresher.refresh()` 在單一 refresher lock 內收集 provider、呼叫 `correlate_work_sources()`、投影並寫入 `WorkSnapshotStore`。`correlate_work_sources()` 會透過 `load_work_item_overrides(repo_root)` 自行讀取 `.cortex/work-items.yaml`；此次 correlation 沒有回傳該 raw input 的 revision。`WorkSnapshot` 有 `sequence`、`written_at`、provider revisions、sources 與 ownership，`WorkSnapshotStore` 會以 canonical JSON 原子寫入，但 sequence/hash 本身沒有 attempt generation 或成功 correlation 的證明。provider degraded 時可能沿用 last-good data，因此 snapshot row 存在仍不足以證明最新 refresh 成功。

這是 #1064 Monitor producer umbrella 的第二個 slice，硬依賴 #1077 的 durable attempt-generation ledger，也依賴 #1063 已定義的 source/path qualification contract。後續才由 #1064 收束 umbrella、#1065 接 WorkAuthority consumer、#1054 接 Manager admission。這份設計不搬動各票 owner。

## Goals / Non-Goals

**Goals:** 讓成功 manifest 對應實際解析的 override bytes、同代 provider/source revisions 與 durable WorkSnapshot read-back；提供一個無副作用、fail-closed 的 repo/work freshness 查詢。

**Non-Goals:** 實作 #1077 generation allocator/marker store；重做 #1063 source qualification/path admission；修改 WorkAuthority、Manager、CLI、recovery、ship；建立正式 intake 或部署。

## Decisions

### D1 — Parse the exact bytes whose revision is recorded

correlation refresh 對 override file 只讀一次 bytes。以該 bytes 算 `sha256:<lowercase-hex>` 並將同一份 bytes 傳入 parser；缺檔使用明確穩定的 `absent:v1` revision。只有明確的不存在可用 absent revision；permission、read、decode 或 YAML parse error 都是 failed attempt。現有一般讀取入口可保留，但 refresh 路徑不得再另讀一次檔案。

### D2 — Build a per-repo manifest from one refresh candidate

每個 repo 的暫存 manifest 由該次 `WorkModelRefresher.refresh()` 的 provider 結果及 correlation candidate 建立：記錄實際使用的 `provider_id → revision` 與完整 `source_id → revision`，並保存 correlation input revision。source revisions 必須來自將要寫入的同一份 `WorkSnapshot` candidate，source 的 provider 必須出現在同代 provider manifest。任何 required provider 被 `_retain_last_good()` 保留為 degraded、required revision 缺席或 freshness 超過設定上限，都禁止該 repo 成功；不把舊來源版本混成新 generation 的 consumed set。

### D3 — Snapshot first, verified marker last

先依 #1077 contract 取得目前 `running` generation；#1078 不分配 generation，也不建立平行 marker store。刷新流程將 `WorkSnapshot` 寫入 `WorkSnapshotStore` 後，從 durable path reload，使用 store 的 canonical serialization 計算/核對 digest，並驗證 sequence、目標 repo/work rows、source ownership、provider/source revisions 與本次 candidate 一致。成功發布必須是 #1077 store 提供的窄操作，帶 expected generation 並確認該代仍是 `running`；它將 #1078 的 input/source/snapshot evidence 與 `succeeded` 原子地綁定。generation 已改變或 read-back 任一欄不同時不寫成功。snapshot write 與 marker update 間的 crash 留下 running/failed attempt，舊成功不可代替最新 attempt。

### D4 — Use one trusted freshness read path

在 `WorkModelRefresher` 提供唯一 repo/work API（例如 `trusted_correlation_freshness(repo, work_id)`）。它只接受身份參數；repo root 取自 Monitor 最近一次 `ProjectState` 集合，沿用 refresh 的 canonical-root 選擇規則，無唯一可信 root 時回 untrusted。API 從 canonical root 重新讀取目前 override bytes，以 D1 同一 revision 規則驗證 input；再讀 #1077 的 latest per-repo attempt、同代 success manifest 和 durable snapshot。API 核對 generation、snapshot digest/sequence、目標 row/ownership、provider/source revision map、required provider status/freshness，以及 snapshot/provider age 不超過 `stale_after_seconds`。API 不執行 refresh、不寫 marker、不修復或 backfill 舊資料。

### D5 — Return typed, stable failure reasons

結果是 immutable typed trust verdict，至少帶 `trusted`、穩定 reason code 與已驗證的 generation/input/source/snapshot evidence。reason codes 分別辨識 no-root/ambiguous-root、missing/legacy/unknown/running/failed marker、malformed evidence、input mismatch、source/provider mismatch、snapshot missing/drift、generation mismatch、stale 與 I/O error。若同時有多個問題，採用固定優先序，避免同一份狀態在不同呼叫順序回傳不同原因。failure 結果不回報未驗證資料為 trusted evidence。

### D6 — Keep WorkSnapshot compatibility and fail closed on old state

不需改 `work-items-snapshot/v1` 的 row schema；成功 evidence 放在 #1077 的 generation marker contract 中。舊 snapshot 可保留供 listing/diagnostic 使用，但沒有 matching success manifest/read-back 綁定時 freshness API 一律回傳 untrusted，不自動升級或重建 evidence。`WorkSnapshot.sequence` 仍只代表 snapshot 順序，不能當 attempt generation。

### D7 — Keep the dependency boundary explicit

#1077 owns generation allocation、running/failure persistence 與 marker storage；#1078 consumes its exact contract and adds success evidence through the narrow same-generation transition. #1078 不複製 ledger writer。如果 #1077 未提供 expected-generation success publication seam，開始實作前先在兩票的 interface contract 對齊；不得在此票偷偷補一套 allocator/store。#1063 owns source qualification and path admission; #1065 and #1054 remain consumers/gate owners.

## Risks / Trade-offs

- **Snapshot 與 attempt marker 是分開 durable writes：** running 先寫、snapshot read-back 完成、成功 marker 最後發布。中間故障會降低可用性，但不會讓舊成功冒充最新成功。
- **Provider last-good 保留造成表面完整：** manifest 以同代 candidate 的 provider status/revision 判斷，degraded source 不會變成成功證據。
- **Override 在 refresh 後改變：** freshness query 以目前 raw bytes revision 比對；不符即回 input-mismatch，直到完整成功刷新。
- **舊 snapshot 沒有 marker evidence：** 繼續提供診斷/列表相容性；trusted API 保守回 untrusted，等待完整新 refresh。

## Open Questions

無。#1077 success-publication operation 是本票開始實作前的硬契約前置，不由本票自行代做。
