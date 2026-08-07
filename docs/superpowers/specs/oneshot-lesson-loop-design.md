---
status: accepted
work_item: oneshot-lesson-loop
---

# oneshot-lesson-loop Design

## Decisions

### D1 outcome ledger 落點——新檔 `track_record.py`，不擴充既有三個「看似相關」模組

新增獨立模組 `paulsha_cortex/coordinator/track_record.py`（實作票落點，本票不寫程式
碼），不擴充下列任一既有模組：

| 既有模組 | 實際語意 | 為何不適合承載 track_record |
|---|---|---|
| `gate_ledger.py` | 由 manager 掌控 wrapper 執行、模型結束**之後**才產生的**確定性 gate 通過/失敗**紀錄（R2 重驗，模型不可自述、不可控） | fail-closed 契約很緊；混入「one-shot 整體品質」這種較軟的分類語意，會污染既有 gate pass/fail 的精確性 |
| `completion.py` 的 `CompletionRecord.sizing_score`／`sizing_band` | work item **複雜度屬性**的快照（`#222`／design `#208` H.2），每次 repair／re-claim 重算 | 衡量的是「這件事有多難」，不是「這次做得好不好」；band 是輸入特徵，不是 track_record 要的結果標籤 |
| `engineering_outcome.py` | `#275` 落地的「一個 work item 終局落在哪」outbox，`outcome ∈ {shipped, abandoned, rejected, failed, rolled_back}`（`engineering_outcome.py:51`-`:56`） | 語意層級是「work item 級終局」而非「one-shot 嘗試品質」——一個 `shipped` outcome 底下可能已經吃了 2 輪 repair，`shipped` 本身無法回答「這次是 clean 還是 fixup」 |

理由：`gate_ledger.py` 與 `CompletionRecord` 的語意早已被既有測試與呼叫端鎖死，混用
會製造「一個欄位身兼兩種不相干語意」的技術債（正是 issue 偵察階段要避免誤認的兩個
陷阱）。`engineering_outcome.py` 語意上最接近，但粒度不同——見 D5 的化解方案（track_record
**消費**它而非**擴充**它）。

風險與緩解：新檔會是第四個「終局相關」模組（`gate_ledger`／`completion`／
`engineering_outcome`／`track_record`），閱讀成本上升——緩解：本文件的表格即是
「哪個模組管哪個語意」的權威對照，未來任一模組的 docstring 都應反向連結回本節。

### D2 跨 repo 邊界：cortex 只產出 lesson payload，recall 完全屬 hippo（本票最重要的落差修正）

issue 原文 §6「lesson 進 memory(`knowledge/`) → wakeup 召回」把 lesson 儲存與召回都
寫在同一句話裡，容易誤導後續實作者把 hippo 內部邏輯直接搬進 cortex repo。但
`CLAUDE.md` 明文：

> 對 `paulsha-hippo` 維持零 runtime 依賴；僅 `persona/loader.py` 保留 upstream deck
> schema lazy import。

因此本票劃線：

- **cortex 端（本票範圍）**：只定義並（未來由實作票）產出一份結構化 lesson payload
  （R3 已凍結欄位），透過一個新的 append-only 出口寫出到本機檔案系統（比照 `#275`
  `engineering_outcome.py` 的 `OutcomeStore` 慣例：append-only、`outcome_id` 式
  idempotency key、`list`/`show` 唯讀 surface）。
- **hippo 端（不屬本票，不屬 cortex repo）**：讀取該出口檔案、決定怎麼存進
  `knowledge/`、決定 wakeup 時怎麼召回、決定索引與相似度比對方式——這些全部是 hippo
  的內部實作細節，cortex 不 import 任何 hippo 模組，不直接操作 `knowledge/` 目錄。

先例：`engineering_outcome.py` 模組 docstring 已明文「本模組也不 import 任何 hippo
套件——這是外部 learning systems（含 Hippo）消費的唯讀 outbox，Hippo 未安裝時本模組
的一切行為必須維持不變」（`engineering_outcome.py:25`-`:27`）。lesson payload 的出口
SHALL 沿用同一原則，但 MUST 是獨立檔案／schema（見 R3 的「不可複用同一份
`engineering-outcomes/<repo-slug>.jsonl`」），因為 outcome 詞彙與粒度不同，混用會讓
既有 `#275` 消費端（含 Hippo）收到語意混雜的 `outcome` 值。

風險與緩解：「cortex 只產出」的邊界如果實作票沒看到本節，容易照 issue 原文字面誤植
hippo 邏輯——緩解：R3 已把「MUST NOT import hippo／MUST NOT 操作 `knowledge/`」寫成
SHALL 條文，且本節與 `#275` 既有先例互相印證，非本票孤立主張。

### D3 session-health 跨 vendor 缺口——標注待確認，不杜撰路徑

issue §5 提及 `docs/research/05` backlog（「能解析 Claude Code session、產出與 codex
同 schema」），本票查證 `find docs -iname '*research*05*'` 於本 repo **零命中**。此
backlog 極可能存在於外部 `hamanpaul/session-health` repo，而非本 repo。

決議：本文件與未來實作票 MUST NOT 杜撰此路徑；若需要引用，SHALL 標注「待向外部
`session-health` repo 確認位置」，或直接略去具體路徑只描述現象（「Claude Code session
格式的 session-health 解析目前不完整，屬外部 repo 的已知缺口」）。本票不因此阻擋——
R2 已把 `session_health` 定義為不透明 pass-through 欄位，即使某些 executor
（Claude Code）暫時沒有完整 session-health report，`session_health: None` 本身就是
合法值，不影響 outcome 計分（R2 已明文 session-health 不進 reward）。

### D4 棘輪自主度調整的既有掛勾點

候選掛點（供未來實作票選擇，本票只列出，不寫程式碼）：

1. **`paulsha_cortex/coordinator/autonomy.py:421` `default_is_satisfied()`**——目前
   判定來源是「handoff 是否有有效 `CompletionRecord`」，是一個布林謂詞。棘輪訊號若要
   影響「這個 slice 是否可以自動派工」，理論上可以作為 `is_satisfied` 之外的**額外**
   閘門疊加（例如 `ready_units` 呼叫端在 `is_satisfied` 之外再檢查
   `track_record(resource, task_type, scope) ≥ threshold`），但 `default_is_satisfied`
   本身的簽章（`slice_id, handoff_dir, *, repo_root, git_runner`）不含 `resource`／
   `task_type` 參數，直接塞入會破壞既有純粹的「有沒有 CompletionRecord」語意。
2. **`paulsha_cortex/coordinator/autonomy.py:394` `ready_units()`**——目前的三條件
   （`slice_id` 合法 ∧ `dispatch == 'auto'` ∧ `plan` 非空 ∧ `depends_on` 全滿足）是
   **就緒**判定，不是**信任**判定。棘輪的「調高/調低 autonomy 門檻」語意更接近「這個
   `task_type` 允許派到哪個 band／哪個 resource」，與 `ready_units` 的「這個 slice
   結構上齊備了嗎」是正交的兩件事，不應該混進同一個函式。
3. **`paulsha_cortex/coordinator/autonomy.py:447` `dispatch_ready()`**——fan-out 入口，
   對每個就緒單位呼叫 headless `AgentLauncher`。這是「已經決定要派」之後才執行的動作
   點，若棘輪要**否決**某個 band/resource 組合，理論上要在呼叫 `dispatch_ready` **之
   前**、`ready_units` 拿到就緒集**之後**插入一層新的過濾（形狀類似
   `design-model-capability-envelope-spec.md` R1 的 `capable()` 六項判準，`track_record`
   正是其第六項）——即棘輪不是 `autonomy.py` 內部新函式，而是 `#209 capable()` 的
   一個因子，`capable()` 本身再被 `#138` judge 呼叫、`judge` 的呼叫結果才餵給
   `dispatch_ready` 決定要不要對某個 (slice, resource) 組合派工。

**建議**（本票只建議候選掛點，最終落點屬未來實作票）：優先方案 3——棘輪不新開一條
`autonomy.py` 內部路徑，而是作為 `#209 capable()` 第六項判準的實作，被 `#138` judge
消費，`judge` 的輸出（選哪個 resource／要不要延後）才是 `dispatch_ready` 派工前讀到
的訊號。這樣「調自主度門檻」的決策權留在 `#138` judge 這一層，`autonomy.py` 的
`ready_units`／`dispatch_ready` 維持現有的「結構完整性」判定不被污染，與 R4 已定案的
函式簽章相容性（D 相容 `#209`）完全對齊。

### D5 track_record 是既有終局訊號的下游 reducer，非重複捕捉——但需要一個小型 additive 缺口

查證 `_ship_action`（`work_actions.py:3262`）呼叫 `emit_outcome()` 時傳入的
`review={"merge_authorization_hash": ...}`（`work_actions.py:3277`-`:3280`），**未包含**
`fix_rounds`／`finding_count`——即使同一個作用域內 `active.get("repair_rounds", 0)`
（`work_actions.py:3506`）已經算出這個值可用。`_abandon_action`
（`work_actions.py:2308`）同樣不傳 `review`。

這代表：track_record **無法**單純讀 `#275` 既有 outbox 就推導出 `clean`／`fixup`——
`engineering_outcome.py` 現有 record 只夠回答「shipped 還是 abandoned」，不夠回答「是
一次過還是修過才過」。

決議：track_record.py 的設計 SHALL 兩段式——

1. **短期／本票範圍**：track_record 的 R1 判定規則（見 spec R1 表格）直接引用
   `delivery.ReviewLoop.fix_rounds`（`delivery.py:131`）與 `work_actions.py:3506` 的
   `active["repair_rounds"]`，**不假設**這個值已經被 `#275` 的 outbox 帶出來。
2. **未來實作票的建議路徑**：在 `_ship_action`／`_abandon_action` 呼叫
   `emit_outcome()` 的既有兩處呼叫點，**additively** 把 `fix_rounds` 加進 `review`
   dict（例如 `review={"fix_rounds": active.get("repair_rounds", 0), ...}`）——這符合
   `design-task-type-taxonomy-v2-spec.md` R7「實作票如需增欄 MUST 以 additive 方式擴
   充，MUST NOT 改既有欄位語意」的既有紀律，也符合 `engineering_outcome.py`
   `review: Mapping[str, Any] | None` 本來就是自由 mapping 的既有型別。這麼一來
   track_record 就能把 `#275` outbox 當作**唯一**的終局訊號來源（`list_outcomes`／
   `replay_outcomes`，`engineering-outcome-contract-spec.md` R6），不需要另開一條並行
   的終局捕捉邏輯，把 `outcome_id` 的 idempotency 保證也一併繼承過來。

風險與緩解：如果未來實作票忽略這個 additive 缺口、自己在 `work_actions.py` 別處另開
一條捕捉路徑，會產生兩份不一致的「這次 ship 是 clean 還是 fixup」判定——緩解：本節
明文指出精確的插入點（`work_actions.py:3277`／`:2308` 附近）與資料來源
（`active.get("repair_rounds", 0)`），降低實作票自行摸索、繞出第二條路的機率。

## 風險與緩解

- **`cost` 欄位長期空值**：即使 `#325` 已落地 job 級 `usage`，track_record 到
  `(task_type, scope)` 的 join 仍是未來票的工作；若該票被無限期延後，`cost` 會一直是
  `null`——緩解：R1 已把 `cost` 定為 reserved／nullable，棘輪與 lesson 觸發規則（R3／
  R4）皆不依賴 `cost` 是否有值，`cost` 缺席不阻擋核心閉環運作。
- **taxonomy scope 詞典過緊，計分鍵稀疏**：`design-task-type-taxonomy-v2-spec.md`
  受控詞典目前只收錄 7 個 scope，且非本票所有——沿用該票已有的緩解語言：詞典擴充是
  `task-types.yaml` 的 data-only PR，成本低；`ambiguous`（scope 不在詞典）分類本身
  fail-closed 且可觀測，是詞典維護的觸發訊號，不是 track_record 需要另外處理的例外。
- **`#209` 簽章相容性判斷錯誤**：若未來 `#209` 實際落地 `capable()` 時對 `track_record`
  的呼叫方式與本票 R4 假設不同（例如永遠只傳 `task_type` 不傳 `scope`），track_record
  內部仍必須能在 `scope=None` 時給出有意義的邊際化分數——緩解：R4 已明文
  `scope: str | None = None` 為必要的預設參數設計，非事後修補。
- **lesson payload 出口與 `#275` outbox 撞名或撞檔**：若實作票偷懶直接寫進
  `engineering-outcomes/<repo-slug>.jsonl`，會讓 Hippo 等既有消費端讀到非預期的
  `outcome` 值域——緩解：R3／D2 已明文「MUST NOT 複用同一份檔案或 schema」，且給出
  `OutcomeStore` 式獨立實作的具體先例可抄。
- **棘輪掛點選錯，繞出 `#136`/`#138`/`#209` 三閘體系**：若實作票貪快直接在
  `autonomy.py` 加一個獨立的棘輪檢查函式，會與 D4 建議的「棘輪是 `capable()` 第六項」
  分裂成兩條決策路徑——緩解：D4 已明文優先方案與理由，未來實作票的 code review
  checklist 可要求「是否有新增獨立於 `capable()` 之外的棘輪檢查」作為機械檢查點。
