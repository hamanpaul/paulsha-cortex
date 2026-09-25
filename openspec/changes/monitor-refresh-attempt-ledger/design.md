---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
---

# Monitor refresh attempt ledger 設計

## Context

`WorkModelRefresher.refresh()` 以 instance lock 序列化 refresh，從先前的 snapshot 延續 provider 狀態並執行 repo scan/correlation，最後由 `WorkSnapshotStore` 持久化 last-good payload。`WorkReadModelStore.record_refresh_failure()` 的 exception marker 是記憶體狀態；provider degraded path 也會保留 previous source/rows。兩者都不能代表 durable latest attempt。

本 issue 是 #1064 producer chain 的第一個 slice：#1063 擁有 canonical Todo qualification/path admission；#1077 擁有 attempt generation/running/per-repo failure；#1078 依賴本 marker contract，擁有 exact input/source binding、snapshot read-back 與 trusted freshness API；#1064 umbrella 完成後才由 #1065 接 WorkAuthority、#1054 接 Manager admission。

## Goals / Non-Goals

**Goals:** durable monotonic generation；scan 前 `running`；獨立 per-repo failure outcome；restart、unknown marker 與 marker I/O error fail closed。

**Non-Goals:** source qualification/path admission、successful evidence manifest、snapshot read-back、freshness API、WorkAuthority consumer、Manager gate、CLI、recovery、ship、intake 或 deployment。

## Decisions

### D1 — Sidecar marker，不改 WorkSnapshot schema

在 `WorkSnapshotStore` path 附近另存 strict versioned marker；generation/outcomes 不嵌入 `work-items-snapshot/v1`。Marker store 使用可注入 path，確保所有測試只碰 temp stores。

### D2 — Allocate and persist before scan

從 strict-validated marker 遞增 generation，原子持久化 `running` 後才開始任何 provider scan/correlation。首次 empty store 可配置首代；snapshot 已存在但 marker 缺失、或 marker malformed/unknown 時不得 reset。Marker 讀寫失敗使該次 refresh fail closed。

### D3 — Per-repo terminal failure is durable

同一 generation 的 repo outcomes 分開記錄 `failed`、`degraded`、`exception` 與診斷；單 repo outcome 不覆蓋另一 repo。Last-good snapshot 照常保留，marker 由獨立 path 發布。

### D4 — No trusted success in this slice

Crash 留下的 `running` 明確代表未完成。Scan/correlation 已完成但尚未有 #1078 所需 source/input manifest 與 snapshot read-back 時，記為 `awaiting-success-evidence` 或等價 untrusted outcome；不得標 `succeeded`。#1078 可在完成自己的證據核驗後擴充/轉換 marker contract。

### D5 — Single-writer guarantee

同 instance refresh 必須序列化 generation allocation 與 marker publication。若 durable path 可能由多 process 寫入，需使用 durable lock/CAS 或拒絕第二 writer；in-process mutex 本身不構成跨 process 保證。

## Risks / Trade-offs

- `running` marker 寫入後發生 crash 會留下不可信 generation；這會降低 freshness 可用性，但不會讓 last-good rows冒充最新成功。
- 若 marker 損毀或 I/O 不可用，refresh 不能沿用舊 success 推進；診斷要保留具體 failure，等待 operator 修復或後續明確 recovery contract。
- #1078 尚未交付前，本 slice 不提供可信 success；consumer/gate 在後續 issue 完成前仍維持原責任邊界。
