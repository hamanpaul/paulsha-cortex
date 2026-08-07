---
status: accepted
work_item: design-model-capability-envelope
---

## ADDED Requirements

### Requirement: `capable()` 六項判準必須有單一凍結契約

`capable(resource, work)` 判準集合 MUST 凍結為六項合取式：`work.sizing_band ∈
resource.accepts_bands`（來源 `#208`）、`work.invariant_count ≤ resource.invariant_ceiling`、
`work.artifact_classes ⊆ resource.consistency_scope`、`work.acceptance_mode ∈
resource.acceptance_modes`（前三者本票新定義）、`work.required_capabilities ⊆
resource.capabilities`（已落地，`#130`）、`track_record(resource, work.task_type) ≥
threshold`（來源 `#137`，本票只固定簽章）。任一項為否即整體為否；六項 MUST 全部評估並可觀測，
不得因單項為否而跳過其餘的判定紀錄。下游消費者（`#138` judge、
`claim_readiness.capability_probe`）MUST 依此契約實作，MUST NOT 自行增減判準項目。

#### Scenario: 六項判準全數通過

- **WHEN** 一個 work item 的 `sizing_band`／`invariant_count`／`artifact_classes`／
  `acceptance_mode`／`required_capabilities`／`task_type` 皆落在 resource 封套內，且
  track record 達門檻
- **THEN** `capable()` 回傳真，六項判定依據皆可查詢

#### Scenario: 任一項為否即整體為否

- **WHEN** work 的 `invariant_count` 超過 resource 的 `invariant_ceiling`，其餘五項皆為真
- **THEN** `capable()` 回傳假，且判定依據明確指出是 `invariant_ceiling` 項失敗，其餘五項的
  判定結果仍可查詢（不得因第 2 項失敗而略過第 3–6 項的評估）

### Requirement: resource 封套四個靜態欄位必須有凍結型別與複合鍵

resource 封套 MUST 新增四個靜態欄位：`accepts_bands`（`list[str]`，`green`／`yellow`／`red`
子集，非空）、`invariant_ceiling`（`int`，`≥0`）、`consistency_scope`（`list[str]`，
`code`／`test`／`spec`／`openspec`／`changelog`／`docs`／`pr`／`issue` 子集，非空）、
`acceptance_modes`（`list[str]`，`focused_tests`／`repo_gate`／`live_evidence`／
`github_closure` 子集，非空）。四欄位 MUST 掛在 `(executor, model_id)` 複合鍵上，與既有
`model_identities.IdentityRegistry` 的去重鍵一致；缺席任一欄位 MUST 視為該項判準的
observability bypass（比照現行 `claim_readiness.capability_probe` 的
`envelope_unavailable` 語意），MUST NOT 視為空集合或零值。

#### Scenario: 欄位缺席時 bypass 而非拒絕

- **WHEN** 某身分尚未登錄 `invariant_ceiling`
- **THEN** `capable()` 對該身分的第 2 項判準回傳 bypass（可觀測、不計入否決），而非直接判否

#### Scenario: 複合鍵一致性

- **WHEN** 同一 `executor`（如 `agy`）下有多個 `model_id`
- **THEN** 四個封套欄位分別獨立掛在各自的 `(executor, model_id)` 上，互不共用同一份值

### Requirement: 三閘序必須區分「擋」與「排隊」

`eligibility`／`admission`／`routing` 三閘 MUST 依「失敗是否可自癒」判準區分處置：
eligibility（sizing／envelope 判定，失敗終局）MUST 擋；admission（容量／額度判定，失敗暫時）
MUST NOT 擋，落回排隊＋控速；routing（`capable()` 選資源）MUST 在前兩閘皆通過後才執行。
容量閘 MUST NOT 排在 eligibility 之前。

#### Scenario: Red band 工作被 eligibility 擋下

- **WHEN** work item 的 `sizing_band` 為 `red`
- **THEN** eligibility 閘擋下，不進入 admission 排隊，MUST NOT 因容量空出而被派出

#### Scenario: 額度不足時排隊而非拒絕

- **WHEN** work item 通過 eligibility，但目標資源當下 quota／rate 不足
- **THEN** admission 閘排隊控速，MUST NOT 產生終局失敗

### Requirement: topic×band 矩陣的路由語意受 roster 大小約束

topic×sizing band 路由矩陣 MUST 標註其現行語意邊界：當 registry 內具同一 capability
（如 `build`）的身分數為 1 時，矩陣只回答 eligibility（該不該派），MUST NOT 被解讀為具備
routing（派給誰）語意；具該 capability 的身分數 ≥ 2 時，矩陣才產生實際分流效果。

#### Scenario: 單一 builder 身分下矩陣只有 eligibility 語意

- **WHEN** registry 內只有一個具 `build` capability 的身分
- **THEN** topic×band 矩陣的查詢結果只能用於「是否該直接派 build」的判斷，MUST NOT 用於
  「該派給哪個 builder」的選擇（因為候選只有一個）
