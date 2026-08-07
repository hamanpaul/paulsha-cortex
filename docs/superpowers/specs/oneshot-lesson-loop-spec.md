---
status: accepted
work_item: oneshot-lesson-loop
---

# oneshot-lesson-loop Specification

#137：one-shot 成效閉環——`task_type × outcome` 計分表、session-health 診斷邊界、lesson
萃取的介面契約、與棘輪（ratchet）調自主度門檻的介面契約。**本票是設計文件，不實作
`track_record.py`、不實作 lesson reader/writer 本體、不動 `paulsha-hippo`。**

## 背景

issue 原文（2026-06-24 起，多則 comment 補充）主張「站在 RL 光譜最便宜端」：不訓模型，
而是把 one-shot 的成敗結果（`clean`／`fixup`／`fail`）累積成 `task_type × outcome`
計分表，供（a）棘輪調自主度門檻、（b）lesson 萃取供未來 plan 撰寫召回。issue 明確區分
兩個不可混用的軸——**outcome（reward，用於棘輪）**與 **session-health（process 診斷，
用於歸因，不得當 reward 用）**——並在結尾自陳「本 issue 為設計討論記錄，非實作 PR；
落地時再依 OpenSpec 流程開 change」。

`main` 現況（本票查證，非簡報時點）：

- 全 repo `grep -rn "lesson\|ratchet\|track_record\|session.health"` 於 `paulsha_cortex/`
  零命中——`#137` 完全沒有落地任何一部分，含 `docs/superpowers/specs/
  design-model-capability-envelope-spec.md:98` 自己也明載
  `` `#137`（尚未落地；`grep -rn "track_record" paulsha_cortex` 零命中，本票只引用函式
  簽章，不假設其內部實作） ``。
- `#139` taxonomy 契約（`design-task-type-taxonomy-v2-spec.md` R6）**已凍結**#137 的消費
  義務：「`#137` ledger MUST 以 `(type, scope)` tuple 作為 `task_type × outcome` 計分
  鍵」；loader 為 `paulsha_cortex/deck/task_types.py:61` `load_task_types()` 與
  `paulsha_cortex/deck/task_types.py:134` `classify_title()`，值域凍結於
  `paulsha_cortex/deck/task_types.py:13` `TASK_TYPE_VALUES`。
- `#325`（token usage 收進 job record）**已落地**：`paulsha_cortex/coordinator/
  registry.py` 的 job record 新增 `usage`／`usage_raw`／`usage_reason`／`started_at`／
  `exited_at` 五欄位（fail-closed schema 檢查於 `registry.py:474` 與 `:479`，寫入於
  `registry.py:939`／`:959`-`:971`），`paulsha_cortex/coordinator/usage_aggregate.py:15`
  `aggregate_usage_by_run()` 依 `workflow_run_id` 彙總 `input_tokens`／`output_tokens`／
  `cached_input_tokens`／`reasoning_output_tokens` 四欄位，`cortex stat --usage-by-run`
  暴露唯讀查詢（`paulsha_cortex/coordinator/cli.py:135`）。**這改變了本票 cost 維度的
  設計自由度**——見下方 R1 與 deviations。
- `#275`（engineering outcome contract）**已落地**：`paulsha_cortex/coordinator/
  engineering_outcome.py` 是「外部 learning systems（含 Hippo）消費的唯讀 append-only
  outbox」，`_ship_action`／`_abandon_action`（`work_actions.py:3262`／`:2308`）在既有
  終局轉換（`status="done"`／`status="superseded"`）之前 durable 寫入一筆
  `shipped`／`abandoned` record。**這是本票 D2「cortex 只產出、不管 hippo 召回」邊界
  的既有先例**，但其 outcome 詞彙（`shipped`／`abandoned`／`rejected`／`failed`／
  `rolled_back`，`engineering_outcome.py:51`-`:56`）與粒度（一個 work item 的終局轉換，可能
  已含多輪 repair）都與本票的 `clean`／`fixup`／`fail`（一次 one-shot 嘗試的品質）不同
  ——不可直接借用同一份 record 當 track_record 的 outcome 值，需要額外欄位（見 D1/D5）。
- `#209`（model capability envelope）**已落地**：`capable()` 第六項判準已**凍結函式
  簽章型別** `Callable[[Resource, str], float] ≥ float`（`design-model-capability-
  envelope-spec.md:98`），第二參數為 `work.task_type`（單值字串），**不是** `(type,
  scope)` tuple。其設計文件 D9（`design-model-capability-envelope-design.md:203`）已
  明文「`#137` 定案後若簽章不同，只需改本票 R1 表格第 6 列」——本票需要在 R4 給出對齊
  方案（見下）。

`gate_ledger.py`（`paulsha_cortex/coordinator/gate_ledger.py`）記錄的是「這次 wrapper
執行的確定性 gate 通過與否」（R2 重驗語意，模型不可自述、不可控），`CompletionRecord`
的 `sizing_score`／`sizing_band`（`completion.py:262`-`:282`，`#222`／design `#208`
H.2）記錄的是「work item 的複雜度屬性」——**兩者語意都不是「one-shot 整體是否需要
repair 才過關」**，不可誤認為 track_record 已有雛形（issue 偵察已核實，見 notes）。

## Goals

- 凍結 `task_type(type,scope) × outcome(clean/fixup/fail)` 計分 schema，`cost` 欄位
  reserved／nullable，並明定其未來資料來源投影規則（不寫聚合程式碼）。
- 定案 session-health 為「診斷特徵」而非「reward 的一部分」的語意邊界，且明定其資料
  型別為不透明 pass-through（本 repo 不驗證其內部結構語意）。
- 定案 lesson 萃取觸發條件與**cortex 端輸出介面契約**，明文 MUST NOT 觸碰 hippo 內部
  `knowledge/` 目錄。
- 定案棘輪讀取 `(type,scope)` 歷史成功率、輸出「調高/調低 autonomy 門檻」訊號的介面
  契約，且與已凍結的 `#209 capable()` 第六項簽章相容。
- 明載非目標：不實作任何程式碼、不動 hippo、不開 `openspec/changes/**`。

## Requirements

### R1 outcome 計分 schema 凍結

`track_record` 計分鍵 SHALL 為 `(task_type, scope)` tuple，經 `paulsha_cortex.deck.
task_types.classify_title()` 對 work item 的 mapped issue 標題分類取得
（`kind == "matched"` 時的 `(task_type, scope)`；`scope` 可為 `None`，比照
`task_types.py:134` 既有語意）。MUST NOT 自建第二份值域或自行 regex 解析標題——這正是
`design-task-type-taxonomy-v2-spec.md` R1 明文禁止的行為。

> **落差提醒**：`paulsha_cortex/coordinator/workflow.py:420` `WorkflowRun.combo_selection`
> 目前**只持久化 `task_type`（主軸），不持久化 `scope`**（`deck/selector.py`
> `ComboSelection` dataclass 本身沒有 `scope` 欄位）。track_record 的 writer 要拿到
> 完整 `(type, scope)` tuple，**無法只讀 `combo_selection`**，必須在寫入當下對
> mapped issue 標題重新呼叫 `classify_title()`（複用既有 loader，不得另建 regex）。
> 這是本票查證發現、简报未提及的落差，實作票需注意。

`outcome` SHALL 為三態 `clean`／`fixup`／`fail`，且 MUST 有機械推導規則（供未來實作票
直接引用，本票不寫程式碼）：

| outcome | 判定依據（既有機制） |
|---|---|
| `clean` | ship 終局轉換時，`delivery.ReviewLoop.fix_rounds`（`delivery.py:131`，於 `work_actions.py:3506` 讀作 `active.get("repair_rounds", 0)`）＝`0` |
| `fixup` | ship 終局轉換達成，但 `fix_rounds ≥ 1`（`delivery.py:39` `MAX_FIX_ROUNDS`／`#218` `repair_budget_for_band()` 之內完成） |
| `fail` | run 走 `_abandon_action`（`status="superseded"`）終局，或 repair budget 耗盡進入 `needs_human`（`delivery.py:234` `"copilot-finding-budget-exhausted"`）且未再恢復 |

`cost` SHALL 為 reserved、nullable 欄位。**`#325` 已於本批落地**（job record 的
`usage`／`usage_raw` 欄位、`usage_aggregate.aggregate_usage_by_run()`），本票因此
MUST NOT 再假設「完全沒有資料來源」；但 `usage` 現況只到 **job／`workflow_run_id`**
粒度，尚未有任何程式碼把它 join 到 `(task_type, scope)` 計分鍵——這個 join（一個
`workflow_run_id` 對應唯一一個 `WorkflowRun`，其 `combo_selection.task_type` ＋
重新分類出的 `scope`）SHALL 由未來實作票完成，本票只凍結「`cost` 欄位存在、型別為
`dict|None`、其非空時的內容形狀＝`aggregate_usage_by_run()` 的既有回傳形狀（四個
token 欄位＋`job_count`／`jobs_with_usage`）」，MUST NOT 在本票內撰寫 join 程式碼。

### R2 session-health 為診斷特徵，非 reward 成分

session-health 特徵（issue 原文列舉 `SNR`／`STATE`／`CTX`／`REACT`／`DEPTH`／`CONV`／
`TOOL`）SHALL 只用於**歸因**（lesson 萃取時解釋「為什麼」）與**早期預警**（session 中
途收斂崩壞時的升級求助訊號），MUST NOT 併入棘輪讀取的 `outcome` 計分或以任何形式影響
`clean`／`fixup`／`fail` 的判定——session-health 高但結果錯、或 session-health 低但結
果對，兩種情形皆可能發生，混用會污染 outcome 訊號的純粹性（issue §3 已有此論證，本票
轉為 SHALL 條文）。

session-health report 的產生者是外部 `hamanpaul/session-health` repo，非本 repo 內建
能力。本票的計分 payload SHALL 把 `session_health` 定義為**不透明 pass-through 欄位**
（型別 `dict | None`），本 repo MUST NOT 驗證其內部鍵值的語意正確性，只驗證頂層型別。
跨 vendor 缺口（Claude Code session 格式尚未被該外部 repo 完整支援，issue §5 提及的
`docs/research/05` backlog）**經查證不在本 repo**（`find docs -iname '*research*05*'`
於本 repo 無命中）；本票 MUST NOT 杜撰該檔案路徑，視為外部 repo 的已知 backlog，不在
本 repo 範圍內，不予引用具體路徑。

### R3 lesson 萃取觸發條件與 cortex 端輸出介面契約

觸發條件 SHALL 為以下之一：`outcome == "fail"`；或 `session_health` 存在且其（由呼叫端
定義的）綜合分數低於門檻（門檻數值本票 MUST NOT 凍結，留給實作票依據 `#210` 的自身 run
歷史校準機制決定）。

cortex 端 MUST 只定義**輸出**契約——一個結構化 dict（欄位：`task_type`／`scope`／
`workflow_run_id`／`outcome`／`session_health`（可選，R2 型別）／`lesson`（自由格式
文字或 dict，內容由萃取邏輯決定，本票不規定）／`emitted_at`／`repo`）——透過**新的
append-only 出口**寫出，比照 `#275` `engineering_outcome.py`「外部 learning systems
消費的唯讀 outbox」慣例（append-only、`outcome_id` 型 idempotency key、`OutcomeStore`
式唯讀 `list`/`show` surface），但 MUST NOT 複用同一份 `engineering-outcomes/
<repo-slug>.jsonl` 檔案或同一 schema——两者 outcome 詞彙與粒度不同（見背景段），混用
會讓 `#275` 既有消費端（含 Hippo）收到語意不一致的 `outcome` 值。

cortex 端 **MUST NOT** import `paulsha-hippo` 的任何模組、MUST NOT 直接讀寫 hippo 的
`knowledge/` 目錄——這是 `CLAUDE.md` 明文「對 `paulsha-hippo` 維持零 runtime 依賴」的
契約邊界。lesson 的**召回**（wakeup 階段依 `task_type` 讀回相關 lesson，issue §6）完全
是 hippo range，本票的 spec 只做到「cortex 吐出什麼格式」為止，不規定 hippo 如何儲存
或索引。

### R4 棘輪介面契約

棘輪 SHALL 提供函式 `track_record(resource, task_type: str, scope: str | None = None)
-> float`，讀 `(task_type, scope)` 的歷史成功率（`clean`/(`clean`+`fixup`+`fail`) 或等
價的加權公式，公式本身留給實作票），輸出範圍 `[0.0, 1.0]`。

**與 `#209` 既有簽章相容**：`design-model-capability-envelope-spec.md:98` 已凍結
`capable()` 第六項為 `track_record(resource, work.task_type) ≥ threshold`、型別
`Callable[[Resource, str], float]`。上表簽章刻意讓 `scope` 帶預設值 `None`——呼叫端可
只傳 `task_type`（滿足 `#209` 既有假設，`scope=None` 時 SHALL 邊際化該 `task_type` 下
所有 `scope` 的加權平均），也可傳完整 `(task_type, scope)` 取得更細粒度的分數。本票
MUST 保證省略 `scope` 時的呼叫形狀與 `#209` R1 表格第 6 列一致，不需要 `#209` 改文件；
`#209` 是否要在未來把第六項升級成消費完整 tuple 屬 `#209` 的 follow-up，不在本票範圍。

輸出的「調高/調低 autonomy 門檻」訊號 MUST 接到 `paulsha_cortex/coordinator/
autonomy.py` 既有 dispatch 流程，而非另開一條路——具體候選掛點見
`oneshot-lesson-loop-design.md` D4。

## 非目標

- 不實作 `track_record.py`（或任何同義模組）本體、不實作 lesson writer／reader 程式碼。
- 不撰寫 `usage`→`(task_type,scope)` 的 cost join 聚合程式碼（R1 只凍結欄位形狀）。
- 不修改 `paulsha-hippo` repo 或本 repo內任何 hippo 相關程式碼；不操作 `knowledge/`
  目錄。
- 不處理 issue comment 追加的「coaching / 改進建議」與 `project-usage-assess` 生產力
  ROI 巨觀報表——issue 討論串已把這兩塊併入本 issue 範圍，但為避免這次設計票蔓延到
  「session 品質教練」與「跨類別省時報表」兩個獨立子問題，本票 MUST NOT 展開其設計，
  留待後續票（沿用同一 `(task_type, scope)` taxonomy，不重複發明）。
- 不修改 `#209` `design-model-capability-envelope-{spec,design}.md` 的既有文字（R4 已
  說明相容性不需要對方改動）。
- 不新增 `openspec/changes/**` 底下的 change 目錄——issue 原文明文「落地時再依
  OpenSpec 流程開 change」，本票（設計文件）本身不啟動 OpenSpec 流程。
- 不改 `gate_ledger.py`、`CompletionRecord`（`completion.py`）、`engineering_outcome.py`
  既有程式碼——即便 D1/D5 建議它們是未來 track_record 實作的鄰接／依賴模組。

## 驗收面

- `oneshot-lesson-loop-spec.md`／`oneshot-lesson-loop-design.md` 皆含非空 frontmatter
  （`status`／`work_item`）與非空 Requirements／Decisions 段落，行數量級比照
  `design-task-type-taxonomy-v2-{spec,design}.md`（92／60 行）。
- R1 對 `paulsha_cortex.deck.task_types` 的函式簽章描述與實際 API 一致（可用
  `python3 -c "from paulsha_cortex.deck import task_types; print(dir(task_types))"`
  核對，已於本票撰寫時核對過）。
- R4 的 `track_record()` 簽章與 `design-model-capability-envelope-spec.md:98` 第六項
  的假設型別相容（省略 `scope` 時退化為 `Callable[[Resource, str], float]`）。
- `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的 `#137` 列狀態更新
  為「設計文件已交付」。
- 全套 `pytest` 維持綠燈不倒退（本票不改 `.py`，屬留證性質而非風險緩解）。
