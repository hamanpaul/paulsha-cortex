---
status: accepted
work_item: task-memory-delivery-adapter
---

# 通用 task memory delivery 的 Cortex adapter 規格

本規格對應 [Cortex #857](https://github.com/hamanpaul/paulsha-cortex/issues/857)，依賴 [Hippo #146](https://github.com/hamanpaul/paulsha-hippo/issues/146)。Hippo 擁有通用 payload、候選與事件契約；Cortex 只擁有 host task identity、能力宣告、隔離輸入與 receipt 收集。Cortex 不因自身專案名稱或 workflow card 取得特殊規則。

## Boundary

- 跨 repo 邊界是 versioned public payload/manifest/receipt；不得 import 對方 private Python package 或讀另一方 registry、status、worktree、raw prompt、private note 正文。
- Work Item/WorkflowRun/card 是 Cortex 任務身份來源；Hippo 是 memory candidate 與 legacy strict KPI 的權威。兩邊 failure 不互相改寫對方 lifecycle。
- 內容取得「成功」只表示受 task scope 與 capability 授權的內容已返回；不表示 agent 理解或採用。採用需另附 note id 與實際 action/artifact evidence。
- 歷史 278/222/126/299 聚合數字是動機證據，不是每個 job 的 permission root cause，也不是修復後採用率預測。

## Requirements

### R1 Task envelope

Adapter MUST 產生 `hippo/task-memory/v1` envelope，至少包含穩定 `task_id`（或明示 `session_proxy`）、`attempt_id`、`session_id`、executor/tool、canonical project、goal、task kind、相關檔案／錯誤、capabilities、read scope 與允許 evidence sources。不得由 title、project name 或 workflow-card 名稱猜 task。

### R2 Candidate boundary

Adapter MUST 只消費 Hippo public candidate contract。每次最多 0–3 則 candidate，包含 `note_id`、content hash/version、適用條件、建議行動、來源時間與關聯理由；同一 task 依 note id/hash 去重。Boilerplate、schema、完整 source material 與 candidate 必須可分開識別。

### R3 Capability-aware delivery

Adapter MUST 依 executor capability 選擇下列互斥結果：

1. `context-delivered`：inline/context input 已提供；不得映射為 Read。
2. `snapshot-delivered`：task-scoped、唯讀、受 manifest 綁定的內容快照。
3. `note-fetch`：工具只接受本 task manifest 內的 note id，不接受任意路徑。
4. `ineligible` 或 `read-failed(reason)`：來源不在原任務允許範圍、能力缺失、送達失敗或權限拒絕。

Adapter MUST NOT 將整個 memory root 加入全域權限、使用 allow-all，或反覆推薦已被拒絕的不可讀路徑。

### R4 Tool-neutral evidence

Receipt MUST 能區分 `candidate-selected`、`offer-emitted`、`read-attempted`、`content-returned`、`context-delivered`、`read-failed(reason)` 與 `applied-with-evidence`，並帶 task/attempt/session/tool/project/note/hash/time 關聯。相同 attempt 的重試 MUST 冪等；worker 不直接寫中央 Hippo ledger。

### R5 KPI compatibility

Hippo legacy strict `offer → read → applied` MUST 保留原語意與分母。`context-delivered`、inline、`content-returned` 或 denied attempt MUST NOT 自動成為 legacy Read/Applied；新工具中立取得率另以版本化指標輸出。

### R6 Safety and failure truth

Unsupported major schema、跨 scope candidate、manifest mismatch、無法證實的 permission 層、scope leak、receipt/hash mismatch MUST fail closed 並留下有界 reason。診斷不能授予 authority；不能以單一旗標或 project special case 假定所有 job 同一根因。

### R7 Canary acceptance

每個已支援 delivery path MUST 以隔離 fixture/canary 至少執行 5 次成功與負例，涵蓋 Cortex 固定 permission-denied 案例、不同 repository、不同 task kind、cross-scope rejection、relay 不覆蓋與既有 output schema 相容。eligible authorized content retrieval success rate MUST 達到至少 95%；此 gate 不等同 utility/adoption 成功。

### R8 Lifecycle compatibility

Cortex adapter MUST 從正式 `cortex work intake` 所建立的 Work Item/WorkflowRun 讀取輸入，不使用停用低階 dispatch，不直接修改 registry/ledger，不 recall，不關閉 sandbox/trust-root。Hippo 未安裝或 provider 不可用時，既有 Cortex lifecycle fail-safe 且可觀測。

## Out of scope

- 不實作 Hippo retrieval ranking、knowledge lifecycle、legacy KPI 定義或 Cortex execution engine 本身。
- 不把任何模型名稱寫成全域 invariant；下游 executor/model 沿既有受治理 routing，不能透過 shared config 改寫或繞過 capability gate。
- 不保證所有 executor 具有同一 tool/read 能力；無能力必須回 `ineligible`/`read-failed`，不得用權限放寬補率。
