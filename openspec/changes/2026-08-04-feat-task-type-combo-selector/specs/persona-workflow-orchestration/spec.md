---
status: accepted
work_item: feat-task-type-combo-selector
---

## ADDED Requirements

### Requirement: 工作流建立必須依 task_type 自動選擇 combo 且明示 override 優先

Manager 建立 WorkflowRun 時 MUST 依 #139 taxonomy 的 normalized `task_type` 選出且只選出一個 combo：selector MUST 消費 `paulsha_cortex/deck/task_types.py` 的分類 helper 與 `task-types.yaml` 的 combo 映射，MUST NOT 自行實作標題解析或硬編碼第二份對照。相同輸入的選擇結果 MUST 具決定性。operator 明示 combo（`--combo <id>`）時 MUST 為 authoritative override 且永遠優先於自動選擇；override 的 combo id MUST 經 `load_combo` 驗證，未知或壞損 id MUST fail-closed 拒絕 claim。選出的 combo MUST 經既有 `default_workflow_manifest` compile 與 `validate_manager_spine` 驗證路徑，selector MUST NOT 繞過任何既有 gate。

#### Scenario: fix 標題自動選到 fix-standard

- **WHEN** work item 唯一 mapped issue 的 snapshot 標題為 `fix(deck): 修正選牌`，operator 未明示 combo
- **THEN** 建立的 run `combo` 為 `fix-standard`，provenance 來源為 `task-type-auto`、task_type 為 `fix`

#### Scenario: feat 標題自動選到 feature-oneshot

- **WHEN** 唯一 mapped issue 標題為 `feat(cli): 新增旗標`
- **THEN** run `combo` 為 `feature-oneshot`，provenance 來源為 `task-type-auto`

#### Scenario: 明示 override 優先於自動選擇

- **WHEN** 標題分類為 ambiguous，但 operator 帶 `--combo fix-standard`
- **THEN** claim 成功、run `combo` 為 `fix-standard`，provenance 來源為 `explicit-override`

#### Scenario: override 指向未知 combo 拒絕

- **WHEN** operator 帶 `--combo no-such-combo`
- **THEN** claim fail-closed 並回報具體原因，不建立 WorkflowRun

### Requirement: 選牌訊號必須取自 durable snapshot 的系統事實

task_type 分類輸入 MUST 取自 durable work snapshot canonical row 中 `github_issue` source 的 `title` 欄位；caller 參數 MUST NOT 成為分類輸入。titles 所屬 snapshot 的 canonical hash 與 `authority.snapshot_hash` 不一致時 MUST 視為訊號不可得而走 bypass（reason `snapshot-drift`），MUST NOT 以漂移後的 snapshot 自動選牌。多 issue 時 MUST 依聚合規則：任一分類為 `unknown_type` 或 `ambiguous` → fail-closed；matched 相異 type ≥ 2 → fail-closed；恰一 matched type → 以該 type 查映射；零 matched → bypass。

#### Scenario: snapshot 漂移走 bypass

- **WHEN** claim 當下 snapshot canonical hash 與 authority 凍結的 hash 不一致
- **THEN** selector 走 bypass 沿用現行預設 combo，provenance reason 為 `snapshot-drift`，不使用漂移內容分類

#### Scenario: 多 issue 同 type 正常自動選牌

- **WHEN** work item 映射兩個 issue，標題分別 `fix: A` 與 `fix(deck): B`
- **THEN** 聚合為唯一 matched type `fix`，自動選 `fix-standard`

### Requirement: ambiguous 與 unknown_type 必須 fail-closed 且不建 run

分類為 `unknown_type`（值域外 type 主張）、`ambiguous`（含 scope 受控詞典外）或多 issue matched type 互斥時，selector MUST 拋出帶診斷的錯誤：診斷 MUST 含每個 mapped issue 的標題、分類 kind 與 reason（含合法值域）。claim MUST 失敗且 MUST NOT 建立 WorkflowRun、MUST NOT 猜測或靜默退回任何 combo。

#### Scenario: matched type 互斥 fail-closed

- **WHEN** work item 映射的兩個 issue 標題分別為 `feat: A` 與 `fix: B`
- **THEN** claim fail-closed，錯誤診斷列出兩個 issue 各自的分類明細，未建立任何 run

#### Scenario: 值域外 type fail-closed

- **WHEN** 唯一 mapped issue 標題為 `perf(cli): 加速`
- **THEN** claim fail-closed，診斷列出合法值域，未建立任何 run

### Requirement: bypass 必須沿用現行預設 combo 且可觀測

分類為 `absent`／`unparseable`、matched type 的 combo 映射為 null、或訊號不可得（含 snapshot-drift）時，selector MUST 走 bypass：沿用現行預設 combo（`feature-oneshot`），工作流行為與未接 selector 時完全一致，且 MUST 在 WorkflowRun 的 `combo_selection` provenance 留下來源 `bypass-default` 與具體 reason。`combo_selection` MUST 為 additive 可選欄位並同步 Monitor 投影白名單，providers 投影 MUST NOT 因此 degraded；`cortex stat --combo-selections` MUST 能依來源與 task_type 彙總，使 bypass 比例與缺口 type 可查。

#### Scenario: absent 標題 bypass 有標記

- **WHEN** 唯一 mapped issue 標題無 conventional-commit prefix
- **THEN** run `combo` 為 `feature-oneshot`，`combo_selection` 來源為 `bypass-default`、reason 含 absent 語意

#### Scenario: combo 缺口 type bypass 有標記

- **WHEN** 唯一 mapped issue 標題為 `docs: 補文件`（matched 但映射為 null）
- **THEN** bypass 沿用 `feature-oneshot`，reason 標明缺口 type，`cortex stat --combo-selections` 可見該筆計數

#### Scenario: 投影不因新欄位 degraded

- **WHEN** registry state 中的 run 帶 `combo_selection` 欄位，Monitor providers 掃描該 state
- **THEN** workflow 投影狀態為 ok，非 degraded

### Requirement: fix-standard combo 必須可經 claim 路徑掛載

deck MUST 提供 `fix-standard` combo：以 issue #202 comment 草稿為基底（七卡與 verification／code-review 兩 gate 原樣），補回 `openspec-propose`（define）與 `writing-plans`（plan）兩張 planner 卡以滿足 `validate_manager_spine` 的全 phase spine；草稿移除項（`brainstorming`／`openspec-archive`／`adversarial-review`）MUST 維持移除。combo MUST 通過 `load_combo` schema 驗證，且經 `default_workflow_manifest` 建出的 manifest MUST 通過 `validate_manager_spine`；`task-types.yaml` 的 `fix` 映射 MUST 指向 `fix-standard`。

#### Scenario: fix-standard 可載入且過 manifest 驗證

- **WHEN** 以 `fix-standard` 執行 `load_combo` 與 `default_workflow_manifest`
- **THEN** schema 驗證通過，manifest 覆蓋全部七個 phase 且 `validate_manager_spine()` 不拋錯

#### Scenario: fix 映射經 loader 驗證

- **WHEN** `load_task_types` 帶入 combo 對照表載入契約檔
- **THEN** `fix` 的 combo 為 `fix-standard` 且引用驗證通過，其餘缺口 type 維持 null
