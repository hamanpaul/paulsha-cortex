---
status: accepted
work_item: design-task-type-taxonomy-v2
---

## ADDED Requirements

### Requirement: task_type taxonomy 必須有單一凍結真相源

`task_type` 主軸值域 MUST 為 conventional-commit `type` 六值（`feat`／`fix`／`docs`／`test`／`ci`／`refactor`），凍結於契約檔 `paulsha_cortex/deck/data/task-types.yaml` 與程式凍結常數（雙鎖）。loader MUST fail-closed：未知鍵、空描述、非法 scopes、值域與凍結常數不一致（多值、少值、改名）皆 MUST 拒載並回報具體原因，MUST NOT 靜默取交集或聯集。下游消費者 MUST 經 loader 取得值域，MUST NOT 自建第二份值域；agent-usage-stats 的 repo 級 5 類 MUST NOT 納入 taxonomy。

#### Scenario: 契約檔載入成功

- **WHEN** 以預設路徑載入 `task-types.yaml`
- **THEN** 值域恰為凍結六值，`feat` 映射至 `feature-oneshot`，其餘五值 combo 為 null
- **THEN** scope 受控詞典為 `coordinator`／`porcelain`／`workflow`／`cli`／`deck`／`monitor`／`onboarding`

#### Scenario: 值域漂移拒載

- **WHEN** 契約檔多出 `perf` 或缺少 `refactor`
- **THEN** loader 拒載並回報漂移的值，不靜默調和

### Requirement: 標題分類必須區分 fail-closed 與 bypass

分類 helper MUST 將 issue 標題判為 `matched`／`unknown_type`／`ambiguous`／`absent`／`unparseable` 五類之一，且處置映射 MUST 完備：`matched` → proceed；`unknown_type`（prefix 形式但 type 不在值域）與 `ambiguous`（含 type 合法但 scope 在受控詞典外）→ fail-closed，下游 MUST 拒絕自動決策；`absent`（無 prefix）與 `unparseable`（prefix 不合文法）→ bypass，落回明示路徑且 MUST 可觀測。判準 MUST 為「標題是否明確主張了 taxonomy 語彙」。

#### Scenario: 未知 type fail-closed

- **WHEN** 標題為 `perf(cli): 加速掃描`
- **THEN** 判為 `unknown_type`，處置 fail-closed，理由列出合法值域

#### Scenario: 受控詞典外 scope fail-closed

- **WHEN** 標題為 `fix(claimx): 修正`，`claimx` 不在受控詞典
- **THEN** 判為 `ambiguous`，處置 fail-closed

#### Scenario: 無 prefix 標題 bypass

- **WHEN** 標題為 `修 monitor 掃描漏洞`
- **THEN** 判為 `absent`，處置 bypass，由下游落回明示路徑

### Requirement: combo 對應必須為輸出投影且缺口明示

每個 type MUST 有 `combo` 欄位（既有 combo id 或 null）；非 null 值 MUST 指向既有 combo，loader 帶入 combo 對照表時 MUST 對未知引用拒載。combo 為 null 的 type MUST 由下游以可觀測 bypass 處理，MUST NOT 猜測替代 combo。deck combo 檔既有 `task_type` 欄位（`feature`／`mcu-feature`）為 legacy workflow-shape 標籤，MUST NOT 當作 taxonomy 值域。

#### Scenario: combo 缺口明示為 null

- **WHEN** 查詢 `fix` 的 combo 映射
- **THEN** 得到 null，語意為「缺口明示、下游走可觀測 bypass」，而非任何預設 combo

#### Scenario: 非法 combo 引用拒載

- **WHEN** 契約檔將某 type 的 combo 指向不存在的 combo id，且載入時帶入 combo 對照表
- **THEN** loader 拒載並回報該引用
