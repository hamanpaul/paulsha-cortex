---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# design-task-type-taxonomy Specification

#139：定案 `task_type` taxonomy 契約（主軸 conventional-commit `type`、次軸 `scope`），並落地輕量契約骨架（taxonomy 契約檔＋loader／分類 helper＋測試），作為 #202／#137／#138／#204 的單一真相源。

## 背景

repo 內同時存在至少四種互不相干的「task 分類軸」：deck combo 的 workflow-shape（`feature`／`mcu-feature` 兩值，見 `paulsha_cortex/deck/data/combos/`）、conventional-commit `type`（issue 標題慣例）、conventional-commit `scope`（元件軸）、agent-usage-stats 的 repo 級 5 類。因為沒有任何 issue 明確擁有「task_type 由我定義」的權責，#139、#202、#137、#138 陷入循環等待。

使用者已於 2026-07-27 裁決（見 issue #139 comment）：主軸採 conventional-commit `type`（`feat`／`fix`／`docs`／`test`／`ci`／`refactor`），以 issue 標題 prefix 機械解析、不依賴模型推理；**#139 為 taxonomy 所有者**。依同一決策 comment「如無異議即一併成立」順推成立並由本 spec 凍結：次軸為 `scope`、deck combo 的 workflow-shape 是分類的**輸出**而非輸入、agent-usage-stats 5 類因粒度為 repo 級排除於 taxonomy 之外。

實測 68 張 issue 中 `fix` 佔 24（約 35%）為最大宗，而 deck combo 目前只有 `feature` 與 `mcu-feature`，`fix` 尚無對應 combo——此覆蓋缺口是 #202 選牌器的硬前提。本票以「combo 欄位可為 null、null 即 bypass」明示缺口，不在本票補 combo（#202 已定為 additive with fallback，缺口不再阻擋其落地）。

## Goals

- `task_type` taxonomy 契約定案並凍結：主軸值域、次軸 scope 受控詞典、分類處置語意（fail-closed vs bypass）。
- 落地收斂且可測的最小契約骨架：契約檔 `paulsha_cortex/deck/data/task-types.yaml`、loader／驗證函式與分類 helper（`paulsha_cortex/deck/task_types.py`）、對應測試。
- 定案下游消費者（#202 selector、#137 ledger、#138 judge、#204 skill ledger）的契約邊界，與統一 log reader／status view 的介面契約草案（只定契約，不實作 reader 與 view）。
- 明載：**本票為 taxonomy 單一真相源；後續 #202／#137／#138／#204 引用此契約，不得自建值域。**

## Requirements

### R1 主軸值域凍結且為單一真相源

`task_type` 主軸值域 SHALL 為 conventional-commit `type` 六值：`feat`、`fix`、`docs`、`test`、`ci`、`refactor`，凍結於契約檔 `paulsha_cortex/deck/data/task-types.yaml` 與程式凍結常數（雙鎖，兩處必須一致）。

下游消費者（#202／#137／#138／#204）MUST 經 loader 取得值域，MUST NOT 自建或硬編碼第二份值域。agent-usage-stats 的 repo 級 5 類（coding／testing／PM／learning／bug）MUST NOT 納入 taxonomy，僅保留展示用途。

### R2 次軸 scope 為受控詞典

次軸 SHALL 為 conventional-commit `scope`，受控詞典初始收錄實測既有七值：`coordinator`、`porcelain`、`workflow`、`cli`、`deck`、`monitor`、`onboarding`。scope 為可選（標題 `fix: ...` 無 scope 即合法）。

詞典擴充 MUST 以修改 `task-types.yaml` 的資料 PR 進行，MUST NOT 在任何消費端旁路新增。type 與 scope SHALL 獨立正交（任一 type 可搭配詞典內任一 scope），以避免 M×N 組合爆炸。

### R3 契約檔載入必須 fail-closed

loader MUST 驗證契約檔結構：未知鍵拒絕、每值描述為非空字串、combo 欄位為 null 或非空字串、scopes 為非空且不重複的合法 token 清單。任一錯誤 MUST 拒載並回報具體原因（比照 `paulsha_cortex/deck/schema.py` 的 `DeckSchemaError` fail-closed 慣例）。

契約檔值域與程式凍結常數不一致（多值、少值、改名）MUST 拒載，MUST NOT 靜默取交集或聯集。

### R4 分類語意必須區分 fail-closed 與 bypass

分類 helper SHALL 將 issue 標題判為五類之一：

- `matched`：type 在值域、scope 在受控詞典內或缺省。
- `unknown_type`：具 conventional-commit prefix 形式，但 type 不在值域（例：`perf(cli): ...`）。
- `ambiguous`：type 在值域但 scope 在受控詞典之外，或其他「有主張而無法解析為唯一合法值」的情形。
- `absent`：標題完全沒有 conventional-commit prefix。
- `unparseable`：有 prefix 形式痕跡但不合文法（例：括號未閉合）。

處置映射 MUST 為：`matched` → proceed；`unknown_type` 與 `ambiguous` → **fail-closed**（下游 MUST 拒絕自動決策，不得猜測、不得靜默改判）；`absent` 與 `unparseable` → **bypass**（落回明示路徑，且 bypass MUST 可觀測）。

判準 SHALL 為「標題是否明確主張了 taxonomy 語彙」：有主張而不合法即 fail-closed，沒有主張即 bypass。

### R5 combo 對應為輸出投影且缺口明示

每個 type SHALL 有 `combo` 欄位（既有 combo id 或 null）。初始映射：`feat` → `feature-oneshot`；`fix`／`docs`／`test`／`ci`／`refactor` → null（現況缺口，明示不猜）。非 null 值 MUST 指向既有 combo id，loader 於帶入 combo 對照表時 MUST 驗證並對未知引用 fail-closed。

deck combo 檔的 `task_type` 欄位（`feature`／`mcu-feature`）為 legacy workflow-shape 標籤，MUST NOT 當作 taxonomy 值域，本票也 MUST NOT 改名或遷移該欄位。下游 selector 遇 combo 為 null 時 MUST 走可觀測 bypass。

### R6 下游消費契約邊界

- **#202 selector**：MUST 消費本票的分類 helper 與 combo 映射——`matched` 時查映射選 combo、fail-closed 類拒絕自動選牌、bypass 類落回明示路徑並發可觀測事件。MUST NOT 自行實作標題解析。
- **#137 ledger（成效閉環）**：MUST 以 `(type, scope)` tuple 作為 `task_type × outcome` 計分鍵。
- **#138 judge（cost-aware dispatch）**：MUST 以 `(type, scope)` tuple 作為路由主軸。
- **#204 skill ledger**：MUST 以同一 tuple 作為 skill 使用歸屬鍵。

### R7 統一 log reader 與 status view 介面契約（草案定案，不實作）

- **log reader 契約**：單一有邊界 reader 掃 `$HOME/.claude`、`$HOME/.codex`、`$HOME/.copilot` 的 session log（單檔 64MB 上限與 mtime 篩選邊界），輸出統一 schema 的 session 紀錄，欄位 SHALL 為：`source_agent`、`session_id`、`project`（經 hippo `project_resolver` 歸屬）、`started_at`、`usage`（tokens／cost）、`transcript_path`。harvest 取 transcript、cost 取 usage、usage-assess 取歸屬，三消費者 MUST 讀同一 schema。
- **status view 契約**：動態 JOIN（非第四個資料倉庫），欄位 SHALL 為：`quota`（餘量／reset）、`rate`（瞬時速率）、`health`（跨 agent 存活）、`track_record`（以 `(type, scope)` 為鍵的成功率）。#138 judge 與 #137 棘輪 MUST 讀同一 view。
- 本票只凍結欄位契約；reader 與 view 的實作屬後續實作票。實作票如需增欄 MUST 以 additive 方式擴充，MUST NOT 改既有欄位語意。

## 非目標

- 不實作 #202 的 combo selector（含 bypass 事件發送機制）。
- 不實作 #137 ledger、#138 judge、#204 skill ledger。
- 不實作統一 log reader 與 status view（R7 只凍結介面契約）。
- 不新增 `fix` 等 type 的 combo，不改既有 combo 檔的 `task_type` 欄位（`fix-standard` combo 是否採納屬叢集另案）。
- 不做舊 issue 的 task_type 反推遷移（#202 落地時再決定強制範圍）。
- 不動 CLI（`cortex deck` 子命令與 help 不變）。

## 驗收面

- `task-types.yaml` 可載入且值域為凍結六值；值域漂移、未知鍵、空描述、非法 combo 引用、非法 scope 詞典皆 fail-closed 拒載。
- 分類 helper 對 `matched`／`unknown_type`／`ambiguous`／`absent`／`unparseable` 五類判定正確，且處置映射（proceed／fail-closed／bypass）可程式化查詢、五類皆有定義。
- spec 與 design 文件明載本票為 taxonomy 單一真相源與 #202／#137／#138／#204 的引用邊界。
- 全套 pytest 通過；既有 deck 載入行為與測試不受影響。
