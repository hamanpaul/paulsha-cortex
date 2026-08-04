---
status: accepted
work_item: feat-task-type-combo-selector
---

# feat-task-type-combo-selector Specification

#202：依 #139 taxonomy 的 normalized `task_type` 在工作流建立時自動選擇 deck combo（`feat` → `feature-oneshot`、`fix` → `fix-standard`），明示 override 永遠優先；ambiguous／unknown_type fail-closed 帶診斷，absent／unparseable 與 combo 缺口走 bypass 沿用現行預設 combo 並留下可觀測標記。

## 背景

production claim 路徑的 combo 目前是寫死的：`work_bridge.default_workflow_manifest`（`paulsha_cortex/coordinator/work_bridge.py:144-157`）無條件 `load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)`（`:146`），`start_canonical_workflow`（`:293`，於 `:334` 呼叫）與測試 fallback `_fallback_workflow_starter`（`paulsha_cortex/coordinator/work_actions.py:1220-1270`）都吃這個結果；manager 的 `start` 分支只原樣轉錄 `manifest.combo`（`paulsha_cortex/coordinator/manager.py:7190`）。deck 雖有 `task_type` 欄位與 combo compiler，任務類型從未影響選牌。

#139（`design-task-type-taxonomy`，W1 已規劃）凍結 taxonomy 契約：主軸 conventional-commit `type` 六值、分類 helper `classify_title` 與 loader `load_task_types`（`paulsha_cortex/deck/task_types.py`）、combo 映射欄位（初始 `feat` → `feature-oneshot`、其餘 null 明示缺口），並明載 #202 selector MUST 消費該契約、不得自建解析。issue #202 comment（2026-07-27 議定）進一步裁決 additive with fallback：只有 ambiguous fail-closed；absent／unparseable bypass 落回現行路徑且必須可觀測——實測 `fix` 佔 24/68（約 35%），若 absent 也 fail-closed，落地當天三分之一工作無法派出。

comment 另附經 `load_combo()`（`paulsha_cortex/deck/schema.py:302`）驗證可載入的 `fix-standard` combo 草稿（7 cards、2 gates）。本票考據 production 掛載點後發現：`default_workflow_manifest` 在 compile 後強制 `validate_manager_spine()`（`paulsha_cortex/coordinator/workflow.py:269-297`），要求 manifest steps 覆蓋全部七個 phase（claim／define／plan／build／verify／review／ship）；草稿缺 define 與 plan phase 卡，直接掛載必以「必須涵蓋完整 phase spine」拒絕。因此 combo 檔以草稿為基底補回 `openspec-propose`（define）與 `writing-plans`（plan）兩張 planner 卡（詳 design D6），草稿裁決的移除項維持移除。

## Goals

- Manager／task intake 建立 workflow 時，依 mapped issue 標題的 normalized `task_type` 選出且只選出一個 combo；相同輸入的選擇結果具決定性。
- 明示 combo override 為 authoritative，永遠優先於自動選擇，且來源可稽核。
- ambiguous／unknown_type fail-closed 帶可行動診斷；absent／unparseable 與 combo 缺口 bypass 沿用現行預設 combo，既有路徑不退化。
- 每次選擇（auto／override／bypass）在 WorkflowRun 留 provenance，`cortex stat` 可回答 bypass 比例與卡在哪些 type，供 #137／#138 消費。
- `fix-standard` combo 落地，且可經 production claim 路徑完整掛載。

## Requirements

### R1 selector 必須消費 #139 契約且不得自建值域

selector SHALL 以 `paulsha_cortex/deck/task_types.py` 的 `classify_title` 取得五類分類與處置、以 `load_task_types`（帶入 combo 對照表）取得 type→combo 映射；MUST NOT 自行實作標題解析，MUST NOT 硬編碼第二份 type→combo 對照（#139 R1／R6 契約邊界）。

combo 映射變更 MUST 走 `paulsha_cortex/deck/data/task-types.yaml` 的資料修改：本票將 `fix` 的 combo 自 null 改為 `fix-standard`；`feat` 維持 `feature-oneshot`；`docs`／`test`／`ci`／`refactor` 維持 null（缺口明示，不猜）。

### R2 訊號來源必須是 durable snapshot 的系統事實

task_type 訊號 SHALL 取自 durable work snapshot（`claim.canonical_work_snapshot_path()`，`paulsha_cortex/coordinator/claim.py:200-203`）canonical row 中 `github_issue` source 的 `title` 欄位（`paulsha_cortex/monitor/work_models.py:58-105` 的 `WorkSource.title`）；caller 參數 MUST NOT 成為分類輸入（明示 override 見 R3，屬選擇輸入而非分類輸入）。titles 所屬 snapshot 的 canonical hash 與 `authority.snapshot_hash` 不一致時，MUST 視為訊號不可得而走 bypass（reason `snapshot-drift`），MUST NOT 以漂移後的 snapshot 內容自動選牌。

多 issue 聚合 SHALL 為：任一標題分類為 `unknown_type` 或 `ambiguous` → fail-closed；`matched` 的相異 type 數 ≥ 2 → fail-closed（多個互斥訊號）；恰一個相異 `matched` type → 以該 type 查 combo 映射；零 `matched`（全部 absent／unparseable、title 缺席、或 row 無 `github_issue` source）→ bypass。

### R3 明示 override 永遠優先且 fail-closed 驗證

operator MAY 於 `cortex work start`／`cortex run work start` 以 `--combo <id>` 明示 combo。override 存在時 selector MUST 跳過自動分類直接採用，provenance 來源標記 MUST 為 `explicit-override`；override 的 combo id MUST 經 `load_combo` 驗證存在且合法，未知或壞損 id MUST fail-closed 拒絕 claim，MUST NOT 靜默退回自動選擇。

`cortex deck compile <combo>` 的既有明示指定路徑（`paulsha_cortex/deck/cli.py:27-28`）不經 selector，維持原樣。

### R4 fail-closed 與 bypass 的處置邊界

fail-closed（`unknown_type`／`ambiguous`／matched type 互斥／override 未知 combo）時 selector MUST 拋出帶診斷的錯誤——診斷 MUST 含每個 mapped issue 的標題、分類 kind 與 reason（含合法值域）；claim MUST 失敗且 MUST NOT 建立 WorkflowRun、MUST NOT 猜測任何 combo。operator 以修正 issue 標題或明示 `--combo` 解除。

bypass（`absent`／`unparseable`／combo 映射為 null／`snapshot-drift`）時 MUST 沿用現行預設 combo（`feature-oneshot`），工作流行為與今日完全一致，且 MUST 留下 R5 的可觀測標記。

selector MUST NOT 繞過既有 deck compile 的 policy gate 與 `validate_manager_spine` 檢查；選出的 combo 一律經 `default_workflow_manifest` 現行 compile／驗證路徑。

### R5 選擇 provenance 必須可觀測

每個新建 WorkflowRun MUST 記錄 `combo_selection` provenance（來源 `task-type-auto`／`explicit-override`／`bypass-default`、task_type、combo、reason），採 `retry_classification` 的 additive 可選欄位模式（`paulsha_cortex/coordinator/workflow.py:358`），並同步 Monitor 投影白名單 `_WORKFLOW_V2_OPTIONAL_ROW_KEYS`（`paulsha_cortex/monitor/providers.py:387-414`）；providers 投影 MUST NOT 因新欄位 degraded（#205 教訓）。

`cortex stat --combo-selections` MUST 依來源與 task_type 彙總 workflow runs（比照 `--retry-classifications` 的彙總模式，`paulsha_cortex/coordinator/cli.py:368-384`），使「多少比例走 bypass、卡在哪些 type」可直接查詢。

### R6 fix-standard combo 必須可經 claim 路徑掛載

新增 `paulsha_cortex/deck/data/combos/fix-standard.yaml`：以 issue #202 comment 草稿為基底——`workflow-claim`、`worktree-isolation`、`tdd-red`、`subagent-build`、`verification`、`code-review`、`policy-commit` 七卡與 verification／code-review 兩個 gate 原樣保留——並補回 `openspec-propose`（define）與 `writing-plans`（plan）兩張 planner 卡，以滿足 `validate_manager_spine` 的全 phase spine 硬性要求（`paulsha_cortex/coordinator/workflow.py:269-297`）。

combo MUST 通過 `load_combo` schema 驗證；`default_workflow_manifest` 以 `fix-standard` 建出的 manifest MUST 通過 `validate_manager_spine`。草稿裁決的移除項（`brainstorming`、`openspec-archive`、`adversarial-review`）MUST 維持移除，`id: fix-standard`、`task_type: fix` 與兩個 gate 的 exists globs MUST 與草稿一致。

## 非目標

- 不實作勝率／outcome scoring（#137）與 cost-aware provider routing（#138）；`combo_selection` provenance 即其消費介面。
- 不改 #139 的值域、五類分類語意與 scope 受控詞典；不動 legacy combo `task_type` 欄位（`feature`／`mcu-feature`）。
- 不為 `docs`／`test`／`ci`／`refactor` 新造 combo（維持 null 缺口、可觀測 bypass）。
- 不改 workflow phase 機：`WORKFLOW_PHASES`、`validate_manager_spine` 的全 phase spine 與 `validate_workflow_phase_transition` 一律不放寬。
- 不做舊 issue 標題的回溯改寫或反推遷移；bypass 語意即「維持今日行為」。
- 不改低階 agent 直派模型；workflow mutation 仍由 Manager 單一寫入。

## 驗收面

- `fix` 標題的 work item 穩定選到 `fix-standard`、`feat` 標題選到 `feature-oneshot`；相同輸入重複執行結果一致。
- 明示 `--combo` override 優先於自動選擇（含標題 ambiguous 時），run provenance 可辨識 `explicit-override` 來源；未知 combo id fail-closed。
- `unknown_type`／`ambiguous`／matched type 互斥皆 fail-closed 帶逐 issue 診斷且不建 run；`absent`／`unparseable`／combo-null／snapshot-drift 皆 bypass 用 `feature-oneshot` 且 `combo_selection` 標記可見。
- `fix-standard` 可載入、可 compile、manifest 通過 `validate_manager_spine`。
- `cortex stat --combo-selections` 彙總可查；帶 `combo_selection` 欄位的 registry 經 Monitor providers 投影不 degraded。
- 全套 pytest 通過；未帶訊號的既有測試路徑行為不變。
