---
status: accepted
work_item: feat-slice-executor-model
---

## ADDED Requirements

### Requirement: Slice spec必須能宣告per-slice builder identity且宣告值經registry驗證

spec frontmatter MUST 接受 optional `executor`/`model_id` 成對宣告（皆非空字串）；僅宣告其一 MUST 產生 `invalid-frontmatter` parse_error 且 field 指向缺漏欄。dispatch 前宣告的 `(executor, model_id)` MUST 存在於 model-identities registry（packaged＋instance custom 合併）；unknown identity MUST fail-closed——該 slice 不建 worktree、不啟動任何 model session、標 `needs_human`，錯誤訊息 MUST 列出可用 identity 清單——且 MUST NOT 靜默退回 fanout 層預設。單一 slice 驗證失敗 MUST NOT 影響同批其他 slice 派工。`EMITTED_FRONTMATTER_FIELDS` MUST 與 runtime 解析契約同步納入兩欄；deck compile MUST NOT 輸出這兩欄。

#### Scenario: 單一 specs-dir 內異質 executor 各自派工

- **WHEN** 同一 specs-dir 內兩個 ready slice 各宣告不同的已註冊 `executor`/`model_id`，operator 執行一次 fanout
- **THEN** 兩個 slice 各自以宣告 identity 構建 launcher 派工，model 進入 job dispatch argv
- **THEN** 各 job row 的 `executor`/`model_id` 記錄宣告值，可供稽核

#### Scenario: 只宣告 executor 未宣告 model_id

- **WHEN** spec frontmatter 只宣告 `executor` 而缺 `model_id`
- **THEN** parse 產生 `invalid-frontmatter` parse_error 且 field 指向 `model_id`，slice 維持 hold 不派工

#### Scenario: unknown identity fail-closed 且不波及同批

- **WHEN** 某 slice 宣告 registry 沒有的 `(executor, model_id)` 對，同批另一 slice 未宣告
- **THEN** 該 slice 不啟動任何 model session、標 `needs_human`，錯誤訊息列出 registry 可用的 `executor/model_id` 清單
- **THEN** 同批另一 slice 照常以 fanout 層預設派工

### Requirement: 未宣告per-slice identity的slice行為位元不變

未宣告 `executor`/`model_id` 的 slice MUST 沿用呼叫端傳入的 fanout 層預設 launcher，dispatch 全路徑（prompt、pinned inputs、worktree、dispatch_head、handoff、commit-required 轉換、`--allow-unsafe` canary 限制）MUST 與現行為位元一致；parse meta 僅新增值為 None 的兩個 key。

#### Scenario: 既有 specs 未宣告 identity

- **WHEN** 既有 spec（無 `executor`/`model_id` 欄位）經 fanout 派工
- **THEN** 使用 fanout 層預設 launcher，派工行為與宣告欄位落地前完全一致
- **THEN** 不觸發任何 model-identities registry 載入或驗證

### Requirement: 批外depends_on必須有顯式分類診斷

依賴診斷 MUST 三分：dep 在本批 metas 內未完成 → `deps-unsatisfied:<id>`（維持現行字串）；dep 不在本批但 handoff 目錄存在該 slice_id 的 manifest → `deps-external:<id>`；dep 不在本批且無任何 handoff trace → `deps-unknown:<id>`。`cortex status` 的 `held[].reasons` MUST 呈現此分類；`cortex ready` 對含 `deps-unknown` dep 的 slice MUST 於 stderr 印顯式診斷且 stdout JSON 與 exit code MUST 不變。cycle 偵測對批外邊 MUST 維持不算環，MUST NOT 因 `deps-unknown` 拒絕整批。

#### Scenario: depends_on 打錯字

- **WHEN** 某 slice 的 `depends_on` 指向一個不存在於本批且無任何 handoff manifest 的 slice_id
- **THEN** `cortex status` 的 held reasons 對該 slice 顯示 `deps-unknown:<id>`
- **THEN** `cortex ready` 於 stderr 印出對應診斷，stdout 輸出與 exit code 不變

#### Scenario: 合法跨 specs-dir 依賴

- **WHEN** 某 slice 依賴另一 specs-dir 已派工過、handoff 目錄留有 manifest 但尚未滿足的 slice
- **THEN** held reasons 顯示 `deps-external:<id>` 而非 `deps-unknown:<id>`
- **THEN** 滿足性判定與釋放行為維持現行（manifest 有效即釋放下游）

#### Scenario: in-batch 未完成依賴維持原字串

- **WHEN** dep 存在於本批 metas 且尚未完成
- **THEN** held reasons 維持 `deps-unsatisfied:<id>`，既有消費者不受影響

### Requirement: request層明確宣告的builder identity必須經registry驗證

fanout／tick／dispatch request 與 periodic tick 在 executor 與 model 皆為明確值（含 daemon default 帶入）時，MUST 於派工前以 model-identities registry 驗證 `(executor, model)`；unknown MUST fail-closed——request 回 error、periodic tick 記 tick error 且本輪不派工——錯誤帶可用 identity 清單且 MUST NOT 啟動任何 model session。model 未指定時 MUST 維持現行為（不做 registry 驗證，executor 白名單照舊把關）。

#### Scenario: fanout request 帶未註冊 model

- **WHEN** operator 送出帶明確 `--executor`／`--model` 且 registry 查無該對的 fanout request
- **THEN** request 以 error 結束並列出可用 identity 清單，未啟動任何 model session、未派任何 job

#### Scenario: 不帶 model 的既有呼叫不受影響

- **WHEN** operator 送出只帶 `--executor`（無 `--model`）的 fanout request
- **THEN** 派工行為與現行完全一致，不觸發 registry 驗證
