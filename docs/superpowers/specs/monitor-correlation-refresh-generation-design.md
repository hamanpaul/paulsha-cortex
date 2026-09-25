---
status: draft
work_item: monitor-correlation-refresh-generation
issue: 1064
---

# Monitor correlation refresh generation 設計

## Decisions

### D1 — 唯一綁定與先後依賴

唯一 planned child owner 是 `hamanpaul/paulsha-cortex` issue 1064，唯一 `work_id` 是 `monitor-correlation-refresh-generation`。本 bundle 由下列 Superpowers views 與自己的 OpenSpec change 組成；每份均帶相同 issue/work item，沒有第二個 work ID 或 sibling mapping：

| Kind | Canonical ref |
|---|---|
| Spec | `docs/superpowers/specs/monitor-correlation-refresh-generation-spec.md` |
| Design | `docs/superpowers/specs/monitor-correlation-refresh-generation-design.md` |
| Plan | `docs/superpowers/workstreams/monitor-correlation-refresh-generation/todo.md` |
| OpenSpec proposal | `openspec/changes/monitor-correlation-refresh-generation/proposal.md` |
| OpenSpec design | `openspec/changes/monitor-correlation-refresh-generation/design.md` |
| OpenSpec capability spec | `openspec/changes/monitor-correlation-refresh-generation/specs/monitor-correlation-refresh-generation/spec.md` |
| OpenSpec tasks | `openspec/changes/monitor-correlation-refresh-generation/tasks.md` |

此唯一文件集合尚未透過 Monitor registry 或正式 Cortex intake 建立 live source binding。本規劃先依賴 #1063 的 qualification/path contract；#1064 是其後的 producer，#1065 消費本 API，#1054 保有 pre-Builder run/claim reconciliation、first-Builder Manager gate、stale direct-resume stop與typed diagnostics。依賴順序為 #1063 → #1064 → #1065 → #1054；本 PR 只建立 #1064 規劃，不宣稱任一 implementation child 完成。

### D2 — 獨立、持久、單調的 attempt ledger

在 `WorkSnapshotStore` 同一 instance-scoped directory 建立 sibling attempt-marker store，schema version 固定，例如 `monitor-refresh-attempt/v1`。marker 至少含 `generation`、`outcome`（`running|succeeded|failed`）、attempt/finish timestamps、per-repo outcomes與 diagnostic；成功 repo outcome 含 snapshot sequence/digest、input revision 及 source revision map。先讀取並嚴格驗證 marker，再 `generation + 1` 原子 durable-write `running`，然後才開始任何 provider/correlation work。重用 snapshot store 已有的 atomic write / fsync 慣例。unknown marker 不歸零也不回退；拒絕 freshness 並保留 operator-visible diagnostic。

attempt outcome 依 repo 計算，因此一個 repo failure 不必抹掉另一個 repo 的本次成功；全 workspace summary 可標 partial。新 attempt 若在某 repo掃描前中止，該 repo 無成功 outcome。單一 refresher lock 串行化 generation allocation 與 publication；若部署允許多個 process 同寫同一 instance store，writer ownership 必須 fail closed 或加入 durable CAS/lock，不能以 process-local lock宣稱跨程序單調。

### D3 — revision 來自實際 correlation input

`correlate_work_sources()` 現在自行載入 `.cortex/work-items.yaml`。實作需讓 parser回傳與 parsed overrides 同一讀取的 raw-byte revision；現有 helper仍維持單一 parse/validation來源。digest 用 SHA-256，缺檔使用固定 absent sentinel；parse error 不產生成功 revision。每個 repo outcome另保存 correlation 所用 provider revisions及 sorted `source_id → WorkSource.revision`。建立marker時由本次相關provider snapshot計算，不採另一輪掃描、caller input、或未綁定的查後補讀。WorkSnapshot不必新增correlation field：marker將generation input/source manifest綁定durable snapshot sequence/digest。

本票不改 #1063 的 source qualification/path guard，也不以改寫 override 檔案的 side effect 等同 Monitor 已讀取其新 revision。

### D4 — two-phase publication 與 source snapshot read-back

每個 repo attempt 的 publication 順序：

1. 將新 generation 的 `running` marker durable-write，先使舊 success 失效。
2. 掃描 canonical repo inputs/providers、做 correlation/projection，收集確切 input/provider/source revision。
3. 完整 candidate 成功時寫入 `WorkSnapshot`，然後透過新的 store instance／`WorkSnapshotStore.load()` 從 durable path 重新讀回。
4. 核對 canonical payload SHA-256、sequence、target repo/work rows、provider statuses/revisions及source ownership；marker以同一generation連結已解析的input/source revision manifest與snapshot digest。
5. 全數一致後將該 generation 的 repo outcome durable-write succeeded，marker 引用 read-back snapshot sequence/digest。

Provider degraded、correlation degraded、exception、投影或 snapshot validation failure 都寫 failed outcome；可沿用現有 last-good rows與 provider data供診斷，但不得成功標記。若 failure marker第二次寫入失敗，先前 `running` generation仍使 API fail closed。若 snapshot已 durable而成功 marker未寫入，該 snapshot 仍只是未採信診斷資料。WorkSnapshot 的 `sequence` 保持現有 event/read-model 語意，不當 generation。

### D5 — 唯一 freshness API 為純讀取 oracle

在`WorkModelRefresher`的Monitor public work API提供唯一 `trusted_correlation_freshness(repo, work_id)` 等價介面。refresher從最近一次service傳入的canonical `ProjectState`集合記錄唯一repo root；缺少、重複或不唯一root一律untrusted。API不接受caller指定路徑或revision；reload marker及WorkSnapshot；verify marker、snapshot、current `work-items.yaml` read-back與target WorkItem sources。source generation透過成功marker把所有revisions綁至一份已核對snapshot digest與同一整數generation。age使用Monitor已配置的 `stale_after_seconds`，檢查snapshot age及每個required provider的success freshness。

結果為 immutable typed record，含 `trusted` 與固定 reason code（例如 `marker-missing`、`marker-unknown`、`latest-attempt-failed`、`input-revision-mismatch`、`snapshot-readback-mismatch`、`source-revision-mismatch`、`snapshot-stale`、`provider-stale`）。提供讀取證據欄位，但不回傳 local absolute root。API不寫狀態、不修復marker、不改 WorkAuthority或 Manager。

### D6 — 格式相容與失敗處理

marker是獨立 sidecar，既有 `work-items-snapshot/v1` payload及last-good row格式不需因本票改版。舊或缺少marker的 snapshots可繼續被既有 listing/debugging API讀取，但新 trusted API回傳 untrusted。不得對舊 `sequence`、`written_at`或空 `last_refresh_error`做隱式升級。Marker資料損壞、I/O錯誤、generation non-monotonic與timestamp不含時區全部 fail closed；不得自動重建成成功狀態。

### D7 — Validation 與分工

本票集中修改 Monitor provider/correlation, marker persistence/read-back 與 read API；預定 production modules 限 `paulsha_cortex/monitor/{work_api,work_snapshot,correlation}.py`。Regression tests與上述七份 planning artifacts / changelog為未來實作交付。#1065只在本票 API合併後改 WorkAuthority reader；#1054只在 #1063 qualification、#1064 generation與#1065 consumer均完成後落地 admission。若 source/path contract需要改動，回到 #1063；若跨出 Monitor或增加第二個 consumer，停下重裁 issue與 sizing。

## Goals / Non-Goals

**Goals:** 讓 Monitor產生可驗證的最新 per-repo correlation attempt outcome；以generation綁定實際override/source revisions及durable WorkSnapshot；單一唯讀freshness API明確拒絕失敗、unknown、mismatch與超齡證據。

**Non-Goals:** WorkAuthority loader規則、Manager admission/claim/dispatch、Todo qualification、path guard或override admission、claim reconciliation、pre-Candidate recovery、Candidate/PR recovery及ship語意；正式run/snapshot操作、live intake、部署與implementation。

## Risks / Trade-offs

- **[兩個 durable files 無法以單一 filesystem rename 原子提交]** → 先 durable-write running，再snapshot，再驗read-back，最後成功marker；任一 crash window 均 fail closed。
- **[marker schema 壞掉會讓Monitor freshness停用]** → 不自動重置 generation；暴露 stable reason與diagnostic，保留snapshot供修復調查。
- **[per-repo input追查需要可信 root resolver]** → 從Monitor已載入的canonical ProjectState/config映射解析；不可讓 caller提供任意root。
- **[舊Monitor snapshot沒有marker]** → listing保留last-good相容；trusted API先回untrusted，須等待一次完整成功refresh後才能恢復。
- **[本PR的planning artifacts仍為draft]** → current official sizing維持draft-derived Red；status只有經獨立接受後才能變更，且intake前重跑官方sizing。

## Open Questions

- 無待 implementation 自由裁定的產品問題。跨程序多writer若在實際runtime path存在，需按 D2 採 durable lock/CAS，不能降低generation保障。
