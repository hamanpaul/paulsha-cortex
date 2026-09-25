---
status: draft
work_item: monitor-correlation-refresh-generation
issue: 1064
---

# Monitor correlation refresh generation 規格

## Requirements

Owner 是 producer umbrella [issue #1064](https://github.com/hamanpaul/paulsha-cortex/issues/1064)，唯一 `work_item` 為 `monitor-correlation-refresh-generation`。本規格由 live issue 與 `origin/main` `6a32a3e5e0af841794f340313c11f60f2999f6ae` 形成 draft；#1063 是前置的 canonical source qualification/path contract；#1077 與 #1078 是依序交付本 producer 的兩個 issue-backed slices；#1065 是 freshness consumer，#1054 保有 Manager admission。status 尚未 accepted，因此不是 freeze、intake 或產品授權。

### R1 — 每次 attempt 都有單調 generation

Monitor 每次開始 Work Item correlation refresh MUST 先配置大於前一筆 durable generation 的整數 generation，並先持久化 `running` attempt marker，再掃描來源。attempt 必須以 `succeeded` 或 `failed` 結束；程序在完成前中止時，持久化的 `running` 仍代表最新 attempt 未成功。generation 不得以 `WorkSnapshot.sequence`、`written_at` 或牆鐘時間替代。

#### 驗收案例

- **WHEN** 連續啟動成功、失敗、再成功三次 refresh
- **THEN** 每次 generation 嚴格遞增，latest marker 分別反映當次 outcome
- **WHEN** Monitor 在 durable `running` marker 後中止
- **THEN** restart 後讀者看到未完成的最新 generation，回報不可採信

### R2 — failure marker 獨立於 last-good payload

最新 attempt marker MUST 以獨立於 last-good `WorkSnapshot` rows 的 durable record 儲存。provider 回報 degraded、correlation input 無效、projection／snapshot 寫入失敗或 refresh exception 均使目標 repo attempt 不成功，且留下可診斷 outcome；可保留 last-good rows 供顯示與診斷，但不得用它們表示最新 correlation 成功。寫入失敗 outcome 時若 marker store 無法完成更新，較早的成功亦不得被解讀成已完成的最新 attempt：`running` marker、memory state 或 marker read error均須 fail closed。

#### 驗收案例

- **WHEN** 已存在 matching Todo row 的成功 snapshot 後續 refresh 失敗
- **THEN** last-good rows 可保留，但最新 generation 為 failed／未完成，trusted freshness 回傳 false
- **WHEN** provider 保留 last-good sources 並標 degraded
- **THEN** correlation 不得把保留的 sources 當成本次成功輸入

### R3 — successful marker 綁定實際 correlation inputs

成功的 repo outcome MUST 記錄本次 correlation 實際消費的 input revision，至少包含該 repo `.cortex/work-items.yaml` 的 SHA-256 revision；檔案不存在時必須使用明確、穩定的 absent-input revision。revision 必須來自送進 parser/correlation 的同一份 bytes，不能在掃描後另讀一份來冒充 consumed revision。記錄也 MUST 包含本次 generation 使用的相關 provider revisions 與 `WorkSource.source_id → revision` 集合；集合須可驗證、排序穩定、不得混合不同 durable source snapshot。

#### 驗收案例

- **WHEN** `.cortex/work-items.yaml` 在成功 refresh 前已變更
- **THEN** marker 記錄 correlation 實際讀取的新 revision，而非舊 snapshot revision
- **WHEN** 任何必要 provider revision、來源 revision 或 input revision 缺失／未知
- **THEN** 該 repo outcome 不得標為成功

### R4 — 成功 publication 必須 read back 同一個 source snapshot

Monitor MUST 先 durable-write 完整 candidate `WorkSnapshot`，再從 durable store 重新載入並核對 canonical payload digest、snapshot sequence、目標 repo rows、provider revisions及source revisions是否吻合本次attempt擷取的manifest；current override revision由同一generation marker保存並與snapshot digest綁定。只有 read-back 全數相符後，才可將同一 generation 的 repo outcome 寫為 succeeded。marker 必須指向 read-back 驗過的 snapshot sequence 與 digest。durable snapshot 與 marker 間若遇 crash、遺失或不一致，結果不可採信；不能以 in-memory candidate 或 `replace_durably()` 的成功返回取代 read-back。

#### 驗收案例

- **WHEN** candidate 被寫入後，read-back 的 bytes／digest／sequence 或來源 revisions 不同
- **THEN** 本次 attempt 不是 succeeded，API 回報不可採信
- **WHEN** durable snapshot 正確讀回但 success marker 寫入失敗
- **THEN** latest attempt 保持 running／failed，不能回退採信前一代成功

### R5 — 提供單一唯讀 trusted freshness API

Monitor MUST 提供一個 repo/work-item scoped freshness API，輸入確切 repo 與 work ID，使用 Monitor 設定的明確 `stale_after_seconds` age 上限，回傳 typed trust result、原因、generation、attempt time、snapshot reference、input revision 與 source revisions。API MUST 以 canonical configured repo root 重讀目前 `.cortex/work-items.yaml` revision，驗證 latest per-repo attempt 成功、source snapshot read-back digest／sequence相符、目前 input revision相符、同一 generation 的來源 revisions 相符，且 snapshot 與必要 providers 均未超齡。API 是唯讀，不接受呼叫端自報 input hash、source map 或 freshness boolean。

#### 驗收案例

- **WHEN** 最新 attempt 成功、目前 input 未變、marker 與 snapshot 同代且均在 age 上限內
- **THEN** 對該 repo/work ID 回傳 trusted result，列出所驗證的 generation 與 revisions
- **WHEN** override 在最新成功 refresh 後被 link/unlink 修改但尚未 refresh
- **THEN** API 比對 live input revision 後回傳 untrusted

### R6 — 缺證與 legacy 狀態一律不可採信

缺少、legacy、unknown-version、malformed、過期、`running`、failed marker，generation 倒退、snapshot digest／sequence 不符、source revision 不一致、目前 input 不符或必要來源 degraded／stale 均 MUST 回傳 untrusted 並提供穩定 reason code。舊 WorkSnapshot 中 matching row、較新的 `written_at`、`sequence`、`last_refresh_error` 空值或 last-good payload hash均不足以推論 trusted freshness；沒有 marker 的 legacy snapshot只可作診斷資料。

#### 驗收案例

- **WHEN** marker 缺席、unknown 或讀取失敗，但 legacy payload 含 matching row
- **THEN** API 回傳 untrusted，不使用 row、age 或 last-good fallback
- **WHEN** marker／snapshot 任一 revision 超齡或不同 generation
- **THEN** API 回傳 untrusted 並指出失敗檢查類別

### R7 — dependency 與 owner 邊界

#1064 producer umbrella MUST 依賴 #1063 提供的 deterministic Todo/source qualification 與 existing-path admission contract；#1077 只交付 durable generation/running/failure ledger，#1078 再將 success 綁定實際 consumed correlation inputs、source snapshot read-back與 freshness API，不複製 qualification、path guard 或寫 override admission。#1064 umbrella僅在兩個 producer slices完成後交付；#1065 才把 freshness API 接入 WorkAuthority reader。#1054 仍獨佔 pre-Builder run/claim reconciliation、first-Builder Manager gate、stale direct-resume stop、typed zero/multiple diagnostics 與無 Builder side effect 的 admission 行為。#1064/#1077/#1078 不實作 claim loader、Manager dispatch/claim gate、Todo metadata qualification、pre-Candidate recovery、#1055 Candidate/PR recovery 或 ship 語意。

#### 驗收案例

- **WHEN** #1064 producer 完成而 #1065/#1054 consumer 尚未完成
- **THEN** Monitor API 可供後續 caller 使用，但 claim／Manager 行為未被本票宣稱修復
- **WHEN** source qualification/path admission 所有權要求變更
- **THEN** 停止本票並重裁依賴與 sizing，不在 Monitor refresh producer 內複製 #1063 contract

### R9 — producer slices 有序交付

Monitor producer MUST 依序完成 #1077 attempt-generation/failure-marker ledger與 #1078 source/snapshot success evidence及freshness API。#1077依賴#1063；#1078依賴#1077；producer umbrella #1064只有在兩者完成後才可關閉。此順序不改 #1065 WorkAuthority consumer與 #1054 Manager admission的 owner。

#### 驗收案例

- **WHEN** 查核 producer issue dependency chain
- **THEN** 順序為 #1063 → #1077 → #1078 → #1064 → #1065 → #1054，且各票只宣稱其列明的 owner boundary

### R8 — deterministic regression oracle

測試 MUST 使用隔離 temporary repo、snapshot/marker stores、fake providers與 fake clock，不依賴 GitHub、模型或正式 Monitor state；必須逐一驗證失敗案例保留 last-good payload 但 trusted API 仍拒絕，以及新 override 成功 refresh 後 generation 前進並被正確 read back。測試也 MUST 覆蓋 unknown marker、超齡 snapshot、來源 revisions 混代及 marker/snapshot read-back drift。

#### 驗收案例

- **WHEN** 執行本票 focused suite
- **THEN** R1–R7 正反例均以 durable bytes、generation、payload保留狀態與 API result 驗證
- **WHEN** 測試讀取正式 path、開啟 GitHub／模型或改寫外部 service
- **THEN** 測試設計不符合本票驗收，須改用隔離 fixture
