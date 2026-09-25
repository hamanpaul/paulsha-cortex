## ADDED Requirements

### Requirement: 既有 Candidate 的 authority restart 必須精確比對

Registry MUST 接受 expected durable registry revision、exact WorkflowRun identity/state、exact gate/evidence/PR refs、empty active-job snapshot，以及已驗證 caller 提供的完整新 WorkAuthority digest。輸入缺漏時 MUST 拒絕，且 MUST NOT 自行解析 Todo、WorkAuthority、GitHub 或 filesystem authority。

#### 情境：caller 提供完整 authority 與 run snapshot

- **WHEN** caller 提供完整的 expected run/job tuple、目前 registry revision 與完整的新 authority digest
- **THEN** registry 以該 exact snapshot 評估 request，不解析 external authority

### Requirement: Authority restart 必須是單次 durable WorkflowRun transition

Registry MUST 在 landed #966 CAS boundary 內比對 expected durable revision 與每個 expected tuple 欄位。完全相符時，MUST 在單一 durable revision 中更新同一個有 Candidate 的 run 之 current claim/source binding，設定 `phase=verify`，只 invalidate verify/review gates、清除 `verified_head`，並附加且只附加一筆 authority-restart audit。MUST 保留 Candidate、build/repair steps、builder jobs/evidence、舊 claim-era bindings、PR refs、run identity 與既有 history。

#### 情境：Exact Candidate run 重跑 verification

- **WHEN** expected revision 與完整 tuple 相符，且沒有 active job
- **THEN** 以一個 durable revision 推進同一個 run 的 authority，且只使 verify/review evidence 失效
- **AND** Candidate、build evidence、舊 bindings、PR refs、run identity 與 history 均不變

### Requirement: Authority restart request 必須冪等重播

Registry MUST 將唯一 request identity 與完整 payload digest 和 transition 一起持久化。Exact request replay MUST 回傳原結果，不得再次 write、audit、attempt 或 gate reset。以變更後的 payload 重用 ID 時 MUST 回傳 typed conflict。

#### 情境：reload 後重播 exact request

- **WHEN** registry restart 後再次送入相同 request identity 與完整 payload
- **THEN** 回傳原結果，不清除更新的 verify/review evidence，也不變更 durable bytes

### Requirement: Authority restart conflict 必須 fail-closed

Registry MUST 拒絕 stale raw revisions、任一 tuple 欄位變更、active jobs、Candidate 缺漏、既有 identity 搭配不同 request payload，以及 persistence failures。遭拒時 MUST NOT 留下嘗試設定的 claim/source binding、已清除的 gate、變動後的 `verified_head` 或 audit partial mutation。Conflict memory recovery MUST 遵循 landed #966 contract。

#### 情境：transition 前 revision 或 tuple 發生 drift

- **WHEN** raw durable revision 或任一 expected run/job 欄位不同
- **THEN** request 回傳 typed conflict，且不持久化 authority restart

### Requirement: Registry transition tests 必須使用隔離 fixtures

Tests MUST 使用真實隔離的 JobRegistry fixtures，並為 success、drift、wrong tuple、active job、persistence failure、exact replay、changed-payload replay 與 reload 後 replay 驗證 raw bytes 和 in-memory state。#983 run/Candidate/PR values MUST 只作 fixture；tests MUST NOT 讀寫 live run、delivery journal、GitHub 或 dispatch queues。

#### 情境：#983 型 fixture 不連結 live state

- **WHEN** 將 #983 run identity、Candidate 與 PR 作為 synthetic fixture values 載入
- **THEN** registry transition cases 只操作 temporary registry state，不執行 external read/write
