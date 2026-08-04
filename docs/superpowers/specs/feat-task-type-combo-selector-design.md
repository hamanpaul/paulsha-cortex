---
status: accepted
work_item: feat-task-type-combo-selector
---

# feat-task-type-combo-selector Design

## Decisions

### D1 selector 是 deck 套件內的純函式，消費 #139 契約

新增 `paulsha_cortex/deck/selector.py`：`select_combo(titles, *, taxonomy, override=None) -> ComboSelection` 純函式與 frozen dataclass `ComboSelection`（`combo_id`／`source`（`task-type-auto`｜`explicit-override`｜`bypass-default`）／`task_type: str | None`／`reason: str`）；fail-closed 分支拋 `ComboSelectionError`（繼承 `DeckSchemaError` 慣例的 fail-closed 錯誤，訊息帶逐 issue 診斷）。分類一律呼叫 `task_types.classify_title`，映射一律讀 `load_task_types(combos=...)` 的結果。

理由：#139 R6 明文 selector MUST NOT 自行實作標題解析；把「分類＋映射→選擇」收成一個無 I/O 純函式，決定性（issue 驗收條件）可以用單元測試直接鎖死，且 #137／#138 未來要重算或稽核選擇時可重放同一函式。落在 deck 套件與 `task_types.py` 同層，不讓 coordinator 長出第二份 taxonomy 知識。

### D2 掛載點是 `start_canonical_workflow`，`default_workflow_manifest` 只加參數

`default_workflow_manifest(work_id, *, change, combo_name="feature-oneshot")`（`paulsha_cortex/coordinator/work_bridge.py:144-157`）以參數取代寫死的 `"feature-oneshot"` 字串（`:146`），預設值維持現行為。selector 在 `start_canonical_workflow`（`:293`）建 manifest 之前（`:334` 前）執行一次，兩條 run 建立路徑（`:345-363` 的 needs_human 路徑與 `:376-397` 的 start action 路徑）共用同一次選擇結果；`current_sizing_snapshot`（`:339-341`）吃 `manifest.combo` 自動跟進。測試 fallback `_fallback_workflow_starter`（`paulsha_cortex/coordinator/work_actions.py:1220-1270`）不接 selector，維持預設參數行為。

理由：考據顯示 `default_workflow_manifest` 是 production 唯一的 combo 決定處（manager `start` 分支只轉錄 `manifest.combo`，`paulsha_cortex/coordinator/manager.py:7190`）；在單一收斂點做選擇，manifest、sizing、registry 三個消費面自動一致。fallback starter 是 test/embedding seam，不具 snapshot 訊號，保持現行為即是 bypass 語意。

### D3 訊號來源＝durable snapshot 的 issue title，hash 交叉比對

新增 `claim.mapped_issue_titles(authority, *, snapshot_path=None) -> dict[int, str | None] | None`：以 `_load_snapshot`（`paulsha_cortex/coordinator/claim.py:206-259`）讀 canonical snapshot，canonical hash 與 `authority.snapshot_hash` 不符即回 `None`（selector 轉 bypass，reason `snapshot-drift`）；一致時自 `(repo, work_id)` 的 canonical row `sources` 取 `kind == "github_issue"` 的 `title`（`paulsha_cortex/monitor/work_models.py:58-105`；`_authority_from_canonical_row` 目前解析 sources 時丟棄 title，`claim.py:492-526`）。legacy row（`mapped_issues` 形式，無 sources）與 title 缺席一律得到空訊號 → bypass。

理由：這是「判準取自系統事實」的既有紀律——授權 claim 的就是這份 snapshot，titles 與 authority 同源同 hash，不打 live GitHub（決定性、離線可重放）、不吃 caller 輸入。hash 漂移走 bypass 而非 fail-closed：漂移是 refresh 時序的良性競態，bypass 保持今日行為且留下可觀測標記，不會用一份未授權的 snapshot 內容選牌。

### D4 明示 override 走 `--combo`，比照 #205 模型鏈覆寫的參數流

`cortex work start --combo <id>`（`paulsha_cortex/coordinator/cli.py:131-153` 的 p_work、`:274-318` 的 request_args 組裝）與 `cortex run work start --combo <id>`（`paulsha_cortex/porcelain/run.py:46-72` 的 `_add_work_options`）新增 optional flag；`control/contract.py` 的 `validate_request` work-action 分支（`:86-151`）對 `start` 增驗：`combo` 存在時必須是 `[a-z0-9][a-z0-9-]*` 非空字串。`manager.apply_work_action`（`paulsha_cortex/coordinator/manager.py:7403-7435`）比照 `extract_model_chain_override`（`:7416`）自 args 取出後傳入 `start_canonical_workflow(combo_override=...)`；`run_auto_claim_scan`（`:7438-7460`）不帶 override，永遠走自動選擇。override 的合法性由 selector 以 `load_combo` fail-closed 驗證（未知 id 拒絕 claim）。

理由：issue 範圍明文「使用者明示 combo 時視為 authoritative override，並記錄 override 來源」；#205 的 run-scoped 模型鏈覆寫已踩出 operator 參數→control 契約→manager→work_bridge 的完整參數流形狀，沿用同一形狀最小驚訝。語法驗證在 contract、語意驗證（combo 是否存在）在 selector，與 #205「語法層抽取、語意層 fail-closed」的分工一致。

### D5 provenance 用 WorkflowRun additive 欄位＋投影白名單＋stat 彙總

WorkflowRun 新增可選欄位 `combo_selection: dict[str, str | None] | None = None`（鍵固定 `source`／`task_type`／`combo`／`reason`），比照 `retry_classification`（`paulsha_cortex/coordinator/workflow.py:358`）／`model_chain_override`（`:388`）的 provenance-only 加法模式：`to_dict`／`from_dict` 走 `payload.get(...)` 可選路徑，`_manager_create_workflow_run`（`paulsha_cortex/coordinator/registry.py:1194`）加同名參數，兩條建立路徑寫入。Monitor 投影白名單 `_WORKFLOW_V2_OPTIONAL_ROW_KEYS`（`paulsha_cortex/monitor/providers.py:387-414`）同步加 `"combo_selection"`。`cortex stat --combo-selections` 比照 `--retry-classifications`（`paulsha_cortex/coordinator/cli.py:69-86`、`:368-384`）以 `run.combo_selection` 彙總 `source × task_type` 計數。

理由：issue comment 明文「缺可觀測 bypass 等於沒做」且 `cortex stat` 要能回答比例；run 欄位讓彙總與 run 生命週期同源，不必另立 evidence 檔案掃描面。#205／#261 的教訓是新欄位漏掛投影白名單會讓整份 projection degraded（`providers.py:310-312` 的 #261 D5 註解明示此雷），因此白名單同步是本欄位的硬性配套，並以投影非 degraded 測試鎖住。

風險與緩解：欄位值全部是 selector 輸出的短字串，不含 caller 自由文字之外的內容；reason 上限 500 字元（比照 abandon reason 的 bounded 慣例）避免膨脹。

### D6 fix-standard 以 comment 草稿為基底，補 define／plan 卡滿足 manifest spine

`fix-standard.yaml` 保留草稿的七卡（`workflow-claim`／`worktree-isolation`／`tdd-red`／`subagent-build`／`verification`／`code-review`／`policy-commit`）與兩個 gate（verification／code-review 的 reports globs）原樣，並於 claim 後插入 `openspec-propose`（define）與 `writing-plans`（plan）成九卡；卡序依 phase 單調排列。草稿移除項 `brainstorming`／`openspec-archive`／`adversarial-review` 維持移除。

理由：草稿只驗證過 `load_combo`（schema 層）；production 掛載點 `default_workflow_manifest` 強制 `validate_manager_spine()`（`paulsha_cortex/coordinator/workflow.py:269-297`），要求 steps 覆蓋全部七個 phase 且 define／plan 綁 planner persona——七卡草稿缺 define／plan，掛上去第一個 fix claim 就會 fail-closed 崩在 manifest 驗證，功能死於落地日。phase 機（`validate_workflow_phase_transition` 逐 phase +1）也不允許跳過 define／plan，放寬 spine 屬 workflow 核心不變量變更，遠超本票範圍。cortex dogfood 現實中 fix 工作項目本就產出 spec／plan／openspec change（W1 的 fix-* 批次全套皆有），補回這兩卡與實務一致；「fix 不產生新規格產物」草稿理由裡真正成立的部分（不 brainstorm、不 archive、adversarial 改 band 觸發）全數保留。

風險與緩解：此為對 comment 草稿的最小必要偏離，PR body 與 issue #202 回報此考據結論供追認；RED 測試同時鎖 `load_combo` 與 `validate_manager_spine` 兩層，防止未來有人「還原草稿」而重新引爆。

### D7 fail-closed 不建 run，錯誤走既有 claim 失敗通道

selector fail-closed 時 `ComboSelectionError` 在 `start_canonical_workflow` 內直接上拋：operator 路徑（`cortex work start`）由 work-action 錯誤回報帶出診斷；auto claim scan（periodic）該輪對此 work item claim 失敗、下輪重試，不建立任何 WorkflowRun、不留半成品狀態。

理由：fail-closed 的定義是「拒絕自動決策」——建一個掛 needs_human 的 run 反而需要先給 run 一個 combo（`WorkflowRun.combo` 為必填非空欄位），等於被迫猜測。不建 run 讓 CAS 語意最乾淨：修正標題或帶 `--combo` 後重新 claim 即可。auto scan 對 ambiguous 標題會逐輪重試失敗，屬可見噪音而非靜默吞沒，operator 由 stat／log 可發現；此情境的長期解是修標題（data fix），不值得為它擴 run 狀態機。

## 風險與緩解

- **#139 骨架未落地前本票無法動工**：selector import `paulsha_cortex/deck/task_types.py`（W1 已規劃、與本票同批執行）。緩解：W2 執行序把 `design-task-type-taxonomy` 排在本票前；RED 測試第一條即 import 檢查，缺依賴時立即紅在明確位置。
- **fix-standard 偏離 comment 草稿（7→9 卡）**：D6 已載明是 `validate_manager_spine` 硬性要求所迫的最小偏離；PR 回報 issue #202 追認；若 maintainer 裁決改為放寬 spine，屬另案且不影響本票 selector 邏輯。
- **新欄位使 Monitor 投影 degraded（#205 實際踩過）**：`combo_selection` 與 `_WORKFLOW_V2_OPTIONAL_ROW_KEYS` 同 PR 同動，驗收含「帶新欄位的 registry 投影非 degraded」測試。
- **snapshot 漂移競態導致選擇不穩定**：hash 不符一律 bypass（不猜、不重讀），選擇結果只依（authority snapshot、taxonomy、override）三個輸入決定，決定性測試鎖死。
- **ambiguous 標題讓 auto scan 逐輪失敗刷 log**：屬刻意設計的可見噪音（fail-closed 的代價）；stat 彙總與錯誤診斷都指向具體 issue 標題，operator 修標題後自然收斂。
- **bypass 佔比長期無人看**：`cortex stat --combo-selections` 一條命令可查；後續 #137 ledger 落地時以同一 provenance 欄位做成效歸因，缺口 type 的 combo 補齊有資料依據。
