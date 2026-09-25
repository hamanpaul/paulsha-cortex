---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
---

# Monitor trusted freshness 規格

## Binding

唯一 owner 是 [issue #1078](https://github.com/hamanpaul/paulsha-cortex/issues/1078)，唯一 `work_item` 是 `monitor-trusted-freshness`。本規格、design、Todo 與 `openspec/changes/monitor-trusted-freshness/` 使用同一 binding；#1064 umbrella 仍由 `monitor-correlation-refresh-generation` 擁有。

## Requirements

### R1 — Correlation 必須記錄實際解析的 input revision

每個 repo 的 correlation MUST 以單次讀取取得 `.cortex/work-items.yaml` raw bytes，並以同一批 bytes 計算 SHA-256 revision、解析 YAML 與建立 correlation。檔案不存在時 MUST 記錄穩定的 absent revision。讀取、解碼或 parse 失敗不得產生成功 evidence；不得另讀一次檔案來冒充本次 consumed revision。

### R2 — Success manifest 必須只含同代實際消費的 source revisions

每個 repo 的成功 evidence MUST 記錄本 generation 實際使用的 provider revisions，以及完整 `source_id → revision` map。兩者 MUST 取自同一次 refresh 的 provider/source candidate，不得混入 last-good source、其他 generation 的 provider snapshot 或另一次掃描。required provider/source 缺漏、unknown、degraded 或 stale 時，不得將 repo 標為成功。

### R3 — Durable WorkSnapshot read-back 必須先於成功發布

Monitor MUST 先寫入包含本次 candidate rows 的 WorkSnapshot，再從 durable store reload。read-back MUST 核對 canonical snapshot digest、sequence、目標 repo/work rows、source ownership、provider revisions 與 source revisions。只有全部吻合後，才可把 #1077 attempt ledger 中同一 generation 的 repo outcome 發布為 succeeded，並綁定 input/source manifest 與 snapshot digest/sequence。

### R4 — 只提供一個唯讀 repo/work freshness API

Monitor MUST 提供一個以 repo 與 `work_id` 查詢 freshness 的 public read-only API。API MUST 由 Monitor 最新的 `ProjectState` 集合解析 canonical configured repo root，並使用設定的 `stale_after_seconds`。呼叫者不得傳入 root、input hash、source revisions 或成功旗標。API MUST 驗證目前 input revision、latest attempt generation/outcome、同代成功 manifest、durable snapshot read-back、required provider freshness 與 age bound。

### R5 — 缺證與不一致必須 fail closed

Missing、legacy、unknown、running、failed、malformed、stale、generation mismatch、input/source/snapshot mismatch、ambiguous root 與 I/O error MUST 回傳 typed untrusted result 和穩定 reason code。matching row、`WorkSnapshot.sequence`、last-good payload、空的 `last_refresh_error` 或 in-memory candidate 均不得取代 freshness evidence。

### R6 — 保持 producer/consumer ownership

本票只負責 exact input/source capture、snapshot read-back 驗證、成功 evidence publication 與 freshness API。generation allocation、`running` marker 及 failure/degraded marker writer 由 #1077 提供；source qualification/path admission 由 #1063 提供。不得修改 #1065 WorkAuthority consumer、#1054 Manager gate、CLI、recovery 或 ship semantics。

## Acceptance evidence

隔離測試 MUST 覆蓋成功 refresh 的 input/source/snapshot 綁定、override 改變但尚未 refresh、read-back drift、marker/snapshot 不同代、legacy/unknown/malformed marker、degraded source、超齡資料及 marker/snapshot I/O error。測試使用 temporary stores、fake providers 與 fake clock，不讀寫正式 Monitor state、GitHub 或模型。
