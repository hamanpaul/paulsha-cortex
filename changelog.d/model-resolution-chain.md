# model-resolution-chain

- **`#534` 模型引擎解析優先序顛倒：packaged roster 內建列序壓過人工指定——落實使用者裁決的
  三層解析鏈**。現況與裁決相反：packaged roster 的註解明載「列序即候選優先序：agy 維持
  首位」，planner 因此解析到 `agy/gemini-3.1-pro-high`，而 operator 當日在 host overlay
  宣告的可用引擎清單**根本不含**它；該模型暫時 503 時 define 死亡（`#533` 已把死路修成
  可 recover，但**選錯模型**本身沒修）。新增 `coordinator/model_resolution.py` 作為解析鏈的
  單一真值：

  - **第 1 層 `operator-overlay`（絕對優先）**：host overlay `model-identities.yaml` 宣告
    的身分，**列序即優先序**。解析層是排序主鍵且為 stable sort，因此 `#452` 的 measured
    側寫優先與 `#262` 的 `primary_domain` 偏好原封不動地降級為**同層內**的次要偏好——
    packaged 候選再也沒有機會壓過人工指定。
  - **第 2 層 `evaluated-roster`**：新契約 `$PSC_PROJECT_CONFIG_ROOT/model-eval-roster.yaml`
    （扁平欄位、可手工維護、檔案不存在即空清單）。只有 `verdict: pass` **且**
    `review_status: approved`（且 `reviewer`／`reviewed_at` 齊備）**且**角色列於 `roles`
    的身分才入列——「評估過」不等於「人工核可」。patchmud eval 執行管線本身屬 v4 R2，
    本次只做契約與消費端。清單解析失敗時第 2 層視為空（保守方向，絕不因錯誤多授予資格）
    且**不丟例外**，避免 `#509` 的「一列設定過期打掛整條 tick」重演。
  - **第 3 層 `packaged-fallback`**：packaged roster 降級為候選池，只供評估管線取材。
    政策 `resolution_policy.packaged_fallback`：`allow`（無 overlay 的部署預設）／`warn`
    （有 overlay 時預設，解析落到第 3 層即 fail-loud 打 log）／`deny`（嚴格 fail-closed，
    無候選時錯誤訊息附兩條補救路徑）。
  - **`resolved_model_chain` 改記解析層 provenance**：`source` ∈ `run-override`／
    `operator-overlay`／`evaluated-roster`／`packaged-fallback`，封套來源移到新的選配欄位
    `envelope_source`（`measured`／`default`）。舊值 `default-envelope` 這類不透明值只說得出
    「封套來自預設」，說不出「這顆模型憑什麼進熱路徑」。`#534` 之前寫下的紀錄（legacy
    source、無 `envelope_source`）維持可載入。
  - **兩處寫死的優先序一併移除**：`select_secondary_planner` 過去迭代 `PLANNER_PRIORITY`
    （agy 釘首位，且只認三組 `(executor, domain)`——operator 宣告的 `cg`／新 executor
    planner **永遠不可達**）；`work_bridge` 的 primary planner 寫死 `("codex","claude","agy")`。
    兩者改走三層解析鏈；`PLANNER_PRIORITY` 保留為歷史記錄，不再參與任何選擇。
- **`#509` 殘項：overlay shadow 不再打掛 periodic tick，並補上合法的 operator 覆寫語意**。
  同鍵且逐欄相等＝同一列；內容不同時**以 overlay 為準**（人工指定優先），明示
  `override_packaged: true` 記 info、未明示記 warn 診斷並打 log——不再 `raise ValueError`
  讓 tick 連續失敗開斷路器。新增 `packaged_overrides` 區塊讓 overlay 明示 `park`（完全
  停用、身分仍留在 registry 故 doctor 的 canonical 檢查不破）或 `demote`（降到同層最後）
  packaged 身分；指向不存在的 packaged 身分或與 `identities` 矛盾時 fail-closed。
  新增 `cortex doctor` 的 **`model-resolution` probe**：走與 tick 相同的載入器與排序函式，
  逐 persona 回報生效解析與所在層、明示自己讀的 config root（`#509` 假 PASS 的成因之一
  就是 doctor 與 daemon 讀不同的 config root 卻看不出來），並以不變式守衛「overlay 宣告
  了某角色 → 生效解析必須落在第 1 層」，破壞即 FAIL。
- **`#490`：retry-review 與 manager／tick 改用同一份合併 registry**。
  `review.load_model_identity_registry` 過去走 `use_packaged_default=False` 只讀 host
  overlay，packaged 身分（如 `claude/sonnet`）在 retry-review 被記成
  `reviewer-identity-unknown`；operator 只能把 packaged 那列複製進 overlay，複製回來又踩
  `#509` 的 shadow 中止——死結。兩邊同源後這條矛盾一併消失。
- **`#475` 現場收編為測試 fixture**：operator 於 overlay 宣告的 Claude-compatible 自訂身分
  （`claude/gemma4-26b-a4b-nvfp4`）不得被 packaged 同 executor 身分（`claude/sonnet`）靜默
  取代，解析結果與 `operator-overlay` provenance 皆以測試釘住。executable／launcher 綁定
  屬 launcher 層，仍為 `#475` 的未竟部分。
- `cortex inspect models` 每列加上 `layer=`（`operator-overlay`／`evaluated-roster`／
  `packaged-fallback`／`parked`），JSON 輸出加 `resolution_layer` 欄位。
- 既有部署零遷移成本：所有新能力皆為**選配**欄位／檔案，既有 v1／v2／v3 overlay 檔案不改
  一行也照載、照解析（以測試釘住）。新增 `tests/test_model_resolution_chain_534.py`（29 個
  測試，含 `#490`／`#475` 復現與向後相容不變式）。
