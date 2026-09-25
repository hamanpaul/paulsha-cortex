---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
---

# Monitor refresh attempt ledger 設計

## Context

`WorkModelRefresher.refresh()` 目前以 instance lock 序列化一次 refresh，讀取 previous `WorkSnapshot`、執行 repo providers 與 correlation，最後由 `WorkSnapshotStore` 原子保存 snapshot。Snapshot 是 last-good 工作資料；`WorkReadModelStore.record_refresh_failure()` 對 exception 的紀錄則留在記憶體。Restart 後這兩者不能證明 durable latest attempt 的結果。Repo provider 的 degraded state 會保留舊 sources/rows，因此也需要與 payload 分離的 attempt marker。

owner boundary 依 live issues 與 Draft PR #1072 的 dependency text：#1063 定義 canonical source/path qualification；#1077 保存 attempt generation/running/failure；#1078 才保存成功所需的 exact input/source revisions、snapshot read-back 與 freshness API；其後依序為 #1064 umbrella completion、#1065 WorkAuthority consumer、#1054 Manager admission。

## Goals / Non-Goals

**Goals:** 在 provider scan 前持久化單調 generation；逐 repo 持久化 failure/degraded/exception outcome；marker 錯誤與中斷一律不可信；保留 last-good rows 作診斷。

**Non-Goals:** success manifest、source revision capture、snapshot read-back、trusted freshness API、#1063 qualification/path admission、#1065 WorkAuthority wiring、#1054 Manager run/claim gate、CLI、recovery、ship、正式 intake、deployment。

## Decisions

### D1 — 獨立 versioned sidecar

在注入的 `WorkSnapshotStore` path 附近保存獨立 versioned attempt-marker record；不得把 latest attempt 狀態塞入或推導自 `work-items-snapshot/v1`。marker至少包含 schema/version、generation、attempt status、開始時間，以及 generation 內每個 repo 的 outcome 與診斷摘要。Marker store path 可獨立注入，讓測試只使用隔離的 temp stores。

### D2 — Write running before provider work

Refresh 在取得唯一 writer 後先讀取並 strict-validate marker；generation 遞增後以同目錄暫存檔、flush/fsync 與 atomic replace 持久化 `running`，然後才可呼叫任何 provider scan 或 correlation。Marker 不存在僅在首次 empty-store 初始化時代表 generation 0；已存在 snapshot 而 marker 缺失時視為 legacy/unknown，拒絕重置。Read/parse/write error 都要在掃描前或發生當下 fail closed。

### D3 — 每個 repo 獨立記錄失敗

以 generation 為 attempt identity，逐 repo 更新 outcome；`failed`、`degraded` 與 `exception` 分別保留 outcome type 與穩定診斷。單一 repo 的 failure 不改寫其他 repo 結果。既有 `WorkSnapshot` rows 可保持 last-good，不用 failure marker 改寫或刪除它們。

### D4 — 未完成成功證據時明示 untrusted

Crash 留下的 `running` 保持不變且 untrusted。Scan/correlation 完成但未取得 #1078 的 source/input manifest 與 durable snapshot read-back 時，使用明確的 `awaiting-success-evidence`（或同等非成功終態）；此狀態不得被 freshness API 採信。只有 #1078 可在驗證其成功證據後發布 trusted `succeeded` outcome。

### D5 — Writer ownership 必須涵蓋 store 邊界

同一 refresher instance 的 in-process lock 需覆蓋 marker allocation/publication。若同一 sidecar path 可由多個 process 開啟，實作 MUST 使用 durable lock/CAS 或明確拒絕第二 writer；只靠 Python thread lock 不足以保護跨 process generation。無法取得唯一 writer 時保留最新狀態並拒絕 refresh。

### D6 — 不把 unknown 修成初始狀態

只有可辨認的全新 empty store 可初始化首代。Unknown schema、malformed/truncated bytes、generation 型別或範圍錯誤、讀取權限錯誤、atomic write/replace error 均不得自動修復、覆寫或把 generation 歸零；若失敗寫入前已持久化 `running`，它仍代表未完成 attempt。

## Risks / Trade-offs

- **Marker 與 snapshot 是分開的 durable writes：** `running` 先寫；failure 結果之後寫。中間崩潰只會留下 untrusted 狀態，不會把 last-good rows 當最新成功。
- **Marker 損毀會阻止新 generation：** 保留 failure reason 並 fail closed，避免用自動重建掩蓋 generation 可能倒退。
- **目前不提供 trusted success：** #1077 完成後、#1078 完成前，latest attempt 不能作 freshness authority；依賴順序已明確限制這段行為。
