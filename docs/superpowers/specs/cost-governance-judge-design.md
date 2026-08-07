---
status: accepted
work_item: cost-governance-judge
---

# cost-governance-judge Design

## Decisions

### D1 靜態 resource-inventory：不新開檔案，`#138` 本身不新增靜態欄位

issue §3 原文把供給側靜態 inventory 列為「能力 tier／context window／cost 模型／
autonomy-safety profile」四類；但查證 `#209`（已落地設計，`design-model-capability-
envelope-{spec,design}.md`）D3／R3 已經定案：**不新建 `resource-inventory.yaml`**，
四個新靜態欄位（`accepts_bands`／`invariant_ceiling`／`consistency_scope`／
`acceptance_modes`）additive 掛在既有 `paulsha_cortex/coordinator/data/
model-identities.yaml`（`(executor, model_id)` 複合鍵，schema version 2→3）。這已
覆蓋 issue §3「能力 tier」語意（`capabilities`／`accepts_bands`）。

`#138` 自身查證 main 現況：`context_window`（模型宣告的 token 上限）、
`quota_window_kind`（訂閱制 quota 窗口類型：5h／weekly／premium interaction）、
`autonomy_safety_profile` 三者在全 repo 皆無任何落地或設計欄位（`grep -rn
"context_window\|quota_window\|autonomy_safety"` 於 `paulsha_cortex/`／
`docs/superpowers/specs/` 零命中；唯一命中的 `context window` 字串是
`builder-task-boundary-segmentation-{spec,design}.md` 裡的 turn-failed **錯誤訊息
偵測字串**——`"ran out of room in the model's context window"`，語意是「builder 這次
執行途中撞牆的事後偵測」，與本票要的「resource 靜態宣告值」完全不同概念，命名相似但
不可混用，比照 `#209` D8 對 `acceptance_mode`／`acceptance_surfaces` 的消歧手法）。

**裁定**：MVP judge 規則（見 D6）不需要這三個欄位——`#138` 自己在 issue §4／
cluster todo.md 的四因子分工表裡只認領「rate token」一項，quota／能力／戰績分屬
cost meter／`#209`／`#137`。因此本票 **不** 在 MVP 範圍新增
`context_window`／`quota_window_kind`／`autonomy_safety_profile` 三欄位；若未來
判定 judge 需要更細的靜態資訊（例如用 `quota_window_kind` 正確解讀 `rate`
與 `quota` 兩訊號的交互關係，見 D2），後續實作票 SHALL 遵循 `#209` R3 的既有先例：
additive 擴充 `model-identities.yaml`，MUST NOT 新開第二個 inventory 檔案、MUST NOT
與 `#209` 既有四欄位撞名。

風險與緩解：若後續票誤以為「issue §3 列了四類就要建四類欄位」，會重建
`resource-inventory.yaml` 這個 `#209` 已明文否決的路徑——緩解：本節與 `#209` D3／R3
互相印證，任何要新增靜態欄位的實作票 code review 應先核對「有沒有新開檔案」作為機械
檢查點。

### D2 動態 status view：`#138` 只交付 `rate` 這一格，其餘三格引用既有歸屬

`#139`（已落地設計，`design-task-type-taxonomy-v2-spec.md` R7）已凍結 status view
的欄位契約為 `quota`／`rate`／`health`／`track_record` 四鍵的動態 JOIN（非第四個資料
倉庫）。四鍵歸屬（cluster todo.md 四因子分工表 + `#139` R7 逐一核對）：

| 欄位 | 歸屬 | 落地狀態 |
|---|---|---|
| `quota` | cost meter（`paulshaclaw/cost/`，非本 repo） | 已有實作（跨 vendor quota 取數），未暴露成本票能消費的 view 介面——本票不落地此介接，留給後續實作票 |
| `rate` | **`#138` 本票自己負責** | 全 repo zero 命中（`grep -rn "token_bucket\|rate_limit"` 於 `paulsha_cortex/` 無治理相關命中），本票 D3 定義其資料結構 |
| `health` | `#139` 六項落地任務之一（跨 agent 存活） | 未落地，本票只消費其欄位契約，不重複定義 |
| `track_record` | `#137`（**設計初稿，`feature/137-oneshot-lesson-loop-design` 分支，尚未合併 main**——訂正見文末「與 `#137` 狀態的訂正說明」） | `track_record(resource, task_type, scope=None) -> float`（草稿 `oneshot-lesson-loop-spec.md` R4），型別已與 `#209 capable()` 第六項對齊，但簽章本身尚未定案 |

`rate` 欄位型別本票 SHALL 凍結為：`dict[str, RateSnapshot]`，鍵為
`f"{executor}:{model_id}"`（與 `#209` R2 的 `(executor, model_id)` 複合鍵一致，字串化
只為 view 層可序列化），`RateSnapshot` 至少含：

| 子欄位 | 型別 | 語意 |
|---|---|---|
| `available` | `bool` | 本票 MVP 判斷式唯一實際消費的欄位——「這個 resource 現在還能不能再送一次請求」 |
| `tokens_remaining` | `float \| None` | token bucket 目前餘量，reserved（見 D3），可為 `None`（尚未觀測過此 resource） |
| `window_seconds` | `float \| None` | bucket 補充週期，reserved |
| `last_429_at` | `str \| None`（ISO8601） | 供 D5 429 回授判讀 |

`status view` 是動態 JOIN、不是新資料倉庫——`rate` 子系統（D3）只需要提供一個純函式
`rate_status(executor, model_id) -> RateSnapshot`，被同一個 JOIN 呼叫端與 `quota`／
`health`／`track_record` 並列讀取，不得自建獨立的 CLI 或儲存路徑與其他三格分裂。

風險與緩解：若 `rate` 落地時走出一條與 `quota`／`health`／`track_record` 不同的查詢
介面（例如另開一個 CLI 子命令），會讓 status view「動態 JOIN」的單一入口承諾破功——
緩解：本節明文 `rate_status()` 的純函式簽章，供未來實作票與 view JOIN 層的介接對照。

### D3 rate 自追：新模組 `rate_tracker.py`，token bucket 資料結構

新增獨立模組 `paulsha_cortex/coordinator/rate_tracker.py`（實作票落點，本票不寫程式
碼），不擴充下列既有模組（避免撞責任邊界，比照 `#137` 未合併草稿 D1 的排除表寫法）：

| 既有模組 | 為何不適合承載 rate tracking |
|---|---|
| `paulsha_cortex/coordinator/model_identities.py` | 承載**靜態** identity registry（`IdentityRegistry`），`rate` 是**動態、高頻寫入**的觀測值，混入會讓 fail-closed 的靜態 schema 驗證邏輯與高頻寫入路徑攪在一起 |
| `paulsha_cortex/coordinator/claim_readiness.py` | 承載**一次性交易**（每次 claim 評估一次六項檢查後即結束），`rate` 需要跨多次 claim／dispatch 持續累積狀態，語意上是常駐計數器而非交易快照 |
| `paulsha_cortex/coordinator/manager_daemon.py` | 承載 daemon tick 迴圈與**該迴圈自身**的 backoff（見 D5），不是「每個 resource 各自的請求速率」——兩者粒度不同，混入會讓 `_tick_backoff_seconds` 的單一 daemon-level 退避與多 resource 的個別 rate 狀態糾纏 |

`rate_tracker.py` 的資料結構（本票凍結契約，不寫實作）：

- **token bucket**，鍵為 `(executor, model_id)`（與 `#209` R2 同一複合鍵，一致性優先於
  自創新鍵）。
- 每桶至少追蹤：`capacity`（每分鐘可送出請求數上限，可設定）、`tokens`（目前餘量，
  float，支援分數補充）、`refilled_at`（上次補充時間戳，用於惰性補充計算，不需要背景
  timer thread）。
- `consume(executor, model_id) -> bool`：惰性補充後嘗試扣一個 token，成功回 `True`（可
  送），失敗回 `False`（暫時不可送，觸發 D4 排隊）——純函式風格，狀態經由呼叫端傳入/
  持久化位置留給實作票決定（in-memory dict 供 daemon 單進程使用即可，不必比照
  `registry.py` 的 durable JSON persistence，因為 rate 狀態本質上是短期的、daemon
  重啟後從空桶重新學習是可接受的降級，不是持久事實）。
- `record_429(executor, model_id) -> None`：D5 消費，收縮 `capacity`（乘法退避）並記錄
  `last_429_at`。

風險與緩解：若後續實作票把 rate 狀態誤植為需要跨 daemon 重啟持久化的「事實」（比照
`registry.py` job record 等級的 durability），會過度工程化一個本質上可容忍冷啟動的
觀測值——緩解：本節明文「daemon 重啟後從空桶重新學習是可接受降級」，作為實作票的
明確裁量依據。

### D4 控速分流層：不是 `#136`，是夾在 `autonomy.py` `ready_units()` 與
`dispatch_ready()` 之間的一層新過濾——與 `#137` 未合併草稿 D4 的建議掛點一致

issue §5 原文用詞「控速分流層」容易讓後續實作票誤認為與 `#136`（已落地）是同一層或
要擴充 `#136`。查證 main 現況：`#136` 落地的 `paulsha_cortex/porcelain/capacity_gate.py`
（`evaluate_gate()`）管的是 **Claude Code PreToolUse hook**——互動 session 中 agent
自己手動呼叫 `Task`/`Agent` 或起一個 headless `codex exec`/`claude -p` 時，查
`daemon.idle` 布林決定要不要 `ask`。這是「daemon 忙不忙」這一個軸，與本票要的
「這個 (executor, model_id) 的 quota／rate 是否還有餘裕」是**不同的稀缺資源軸**——
`#136` 忙碌時全域 `ask`，即使某個 resource 的 rate/quota 明明還有餘量；本票的控速
分流不看 daemon 忙不忙，只看 resource 級的額度。**兩者都屬 R4（`#209` spec）的
admission 閘（不擋，只排隊/詢問），是同一閘序位置上的兩把並行鎖，不是同一把鎖的
兩個名字**——後續實作票 MUST NOT 把 `#138` 的控速邏輯塞進
`capacity_gate.evaluate_gate()`，也 MUST NOT 讓 `#138` 重新發明一個 daemon-idle 判斷
（那是 `#136` 已解決的問題）。

真正的掛點（沿用 `#137` design 草稿——未合併分支——D4 建議、本票確認採納的方案 3；
該草稿本身尚未定案或合併，本票僅引其掛點分析作為對齊參考）：

```
manager.ready_units()          -- autonomy.py:394，結構完整性判定（#136/#138 之前）
        |
        v
   【控速分流層（#138 本票）】  -- 新過濾層，本票只定介面，不寫程式碼：
        |                         filter_ready(units, judge) -> (dispatchable, queued)
        |                         judge = D6 的四因子合取式
        v
autonomy.py dispatch_ready()   -- fan-out 入口，只對 dispatchable 呼叫 AgentLauncher
```

介面契約（本票凍結，供未來實作票直接引用）：

```
def filter_ready(
    units: Sequence[ReadyUnit],
    judge: Callable[[ReadyUnit], JudgeResult],
) -> tuple[Sequence[ReadyUnit], Sequence[QueuedUnit]]:
    """依 D6 judge 規則把 ready_units() 的輸出分成「現在可派」與「先排隊」。

    不擋（fail-closed 的「不該派」屬 eligibility，不在本函式範圍——那類已在
    ready_units() 之前被 #208 sizing／#209 capable() 的 eligibility 閘擋掉）。
    QueuedUnit 只是延後，下一輪 tick 重新評估，不寫入任何 terminal 狀態。
    """
```

`dispatch_ready()`（`autonomy.py:447`）簽章本身 MUST NOT 改動——`filter_ready` 插在
`ready_units()` 呼叫端與 `dispatch_ready()` 呼叫端之間，是 **manager tick 迴圈**
（`manager_daemon.py`）裡的一個新步驟，不是這兩個既有函式內部的修改。

**多帳號分流（issue §5「config 已有 haman/arc + claude/codex」）查證結果：main 現況
不成立**——`model-identities.yaml` 全文只有一個身分，全 repo grep `paulc-arc`／
`hamanpaul` 只命中 `mechanical_acceptance/`（policy 語言規範判準，與帳號池無關）。
issue 原文這句描述在 main 上找不到對應設定，**本票不假設它存在**於本 repo；若「多帳號
池」確實存在於某個外部 repo（如 `paulshaclaw`）的設定，那是那個 repo 的落地範圍，本票
只記錄「池總吞吐＝各帳號速率之和」這個**概念**留給 D3 的 token bucket 天然支援（多個
`(executor, model_id)` 各自一個桶，加總即池吞吐），不虛構任何本 repo 不存在的帳號池
config 路徑。

風險與緩解：若實作票把控速邏輯誤塞進 `capacity_gate.py`，會讓一個原本乾淨的
「daemon 忙不忙」判準混入「resource quota/rate 夠不夠」的完全不同語意，兩者未來各自
獨立演進會互相干擾——緩解：本節明文兩者是並行的兩把鎖而非同一把鎖的擴充，
code review checklist 可要求「新的 rate/quota 邏輯是否誤入 `capacity_gate.py`」作為
機械檢查點。

### D5 429 回授：退避公式可重用，退避狀態不可重用——`manager_daemon.py`
`_tick_backoff_seconds` 與本票的粒度不同

查證 `manager_daemon.py:210` `_tick_backoff_seconds(base_interval, consecutive_failures)`
（`TICK_BACKOFF_MAX_EXPONENT = 4`，退避公式 `base_interval * 2**min(exponent, 4)`，封頂
16 倍）：這是 **daemon tick 迴圈自身**的退避——「這一輪 tick 處理失敗了，下一輪 tick
延後多久再試」，狀態只有一份（整個 daemon 一個 `consecutive_tick_failures` 計數器）。

本票要的是 **per-resource** 退避——「`(executor, model_id)` 這個 resource 剛吃了 429，
它自己的 token bucket `capacity` 要收縮多久」，狀態需要一份 per `(executor, model_id)`。
兩者**狀態粒度不同，不可直接重用同一個計數器或函式**，但**退避公式本身**（指數成長、
封頂）值得重用同一個模式，避免發明第三種退避演算法。

裁定：`rate_tracker.record_429()`（D3）SHALL 重用 `_tick_backoff_seconds` 的
**公式結構**（指數成長＋封頂常數），但落地為 `rate_tracker.py` 內部獨立的
per-resource 狀態，MUST NOT 呼叫或依賴 `manager_daemon._tick_backoff_seconds` 本身
（那個函式簽章是 daemon-level 的，硬塞 resource 參數會污染其既有用途）。

issue §7「重用 bot 既有 backoff」提及的 Telegram/bro 側 backoff **本票查無實據**——
`bro` 不在本 repo（`docs/superpowers/workstreams/cost-governance-cluster/todo.md`
未提及任何 bro repo 路徑，本 repo 亦無 `bro`／`telegram` 相關退避程式碼命中）。本票
MUST NOT 杜撰其路徑或介面；後續實作票若要重用 bro 側 pattern，SHALL 先向 bro repo
owner 確認介面存在，若查無則直接採用本節裁定的 `manager_daemon` 公式模式即可，不必
空等一個未經證實的重用來源。

風險與緩解：若實作票誤以為可以直接 import `manager_daemon._tick_backoff_seconds` 並塞入
resource 參數，會破壞該函式現有的 daemon-level 純粹語意——緩解：本節明文「公式重用、
狀態不重用」的精確界線。

### D6 judge MVP 判斷式：四因子合取，interim stub 明確標注

沿用 issue §3／cluster todo.md 已定案的 MVP 形態（`#209` spec 明文「不做成優化器」）：

```
judge(work, resource) =
      rate_available(resource)                          -- D3，本票自己的 rate_tracker
    ∧ quota_remaining(resource) > 0                      -- cost meter（外部，非本 repo）
    ∧ capable(resource, work)                            -- #209 R1 六項合取（已落地設計）
    ∧ track_record(resource, work.task_type) ≥ threshold -- #137 R4（設計初稿，未合併）
```

`judge` 選第一個滿足全部四項的 resource（deterministic 順序，比照 registry 既有迭代
順序），不做評分排序——與 `#209` D1「不做加權評分」、cluster todo.md「已定案」第 5 條
一致。

**interim stub 契約（本票必須明寫，避免實作票誤判「四因子都要等對方落地才能開工」）**：
四個因子目前的實際落地狀態各自獨立，`filter_ready`（D4）落地時 **不需要等四者全部
code-landed**，可先用以下 stub 逐步替換，且每個 stub 都已有明確的「真值」替換點：

| 因子 | interim stub | 真值替換點 |
|---|---|---|
| `rate_available` | 恆真（D3 `rate_tracker.py` 尚未落地） | `rate_tracker.consume()`（設計已定案，本票 D3，等實作票） |
| `quota_remaining` | 恆真（cost meter 尚未暴露 view 介面給本 repo） | 待未來票把 `paulshaclaw/cost/` 接進 D2 status view（設計已定案，本票 D2，等實作票） |
| `capable` | 恆真（`claim_readiness.py:421-437` 現況正是這個 bypass：`capability_lookup is None` 時 `_passed(..., bypass="envelope_unavailable")`） | `#209` R1 的實作票落地 `capable()` 本體後（`#209` 設計已落地 main，等 code） |
| `track_record` | 恆真（`#137` `track_record.py` 尚未落地） | `#137` R4 的實作票落地 `track_record()` 本體後（`#137` **設計本身也尚未落地 main**——未合併分支草稿，需先合併設計、再落地 code） |

四項全恆真時，`judge` 恆選第一個 eligibility 通過的 resource——這與**現況**
（`ready_units()` 之後無任何 admission/routing 過濾，直接 `dispatch_ready()`）行為
等價，即 `filter_ready` 的落地本身即使在四個 stub 全恆真的狀態下也是**安全的
no-op 疊加**，不會讓現有派工行為退化，這也是為何本票主張 D4 的介面骨架可以先落地、
四個因子可以獨立分批替換為真值，不需要一次到位。

風險與緩解：若實作票誤以為 stub 必須全部替換完才能上線 `filter_ready`，會不必要地
把四張獨立的票（本票、`#137` 實作票、`#209` 實作票、cost meter 接線票）綁成一個大
release——緩解：本節明文「stub 全恆真＝現況等價的安全 no-op」，讓 `filter_ready`
骨架可以先落地，後續逐一替換 stub 而不影響既有派工行為。

### D7 session 終止槓桿：只定觸發契約，串接 `#137` `session_health`

沿用 `#137`（**設計初稿，未合併 main**——見文末訂正說明）R2 的既有邊界：`session_health`
是**不透明 pass-through**
欄位（`dict | None`），MUST NOT 併入 outcome/reward 計分，但**可以**用於歸因與早期
預警——本票的 session 終止觸發正是「早期預警」的消費端之一。

`terminate_session` 觸發契約（本票凍結，不寫實作）：

```
def should_terminate(signals: SessionSignals) -> TerminationDecision | None:
    """任一觸發條件成立即回傳非 None 的終止決策；全不成立回 None（繼續跑）。"""
```

觸發來源（issue §6 五項，逐一標註 main 現況）：

| 觸發來源 | main 現況查證 |
|---|---|
| context size 逼近上限 | issue 原文稱「precompact harvest hook 已部分處理」——**本票查無實據**：`grep -rn "precompact\|PreCompact"` 於 `paulsha_cortex/` 零命中；`harvest` 一詞在本 repo 現有語意是 `gate_ledger.py` 的**終局後**收割（`manager.py`／`launcher.py` 註解），與「context 逼近上限時**主動**觸發」的 precompact 語意不同。本票不杜撰該 hook 存在，標注為待確認 |
| checkpoint | `manager_daemon.py`／`claim.py` 已有 phase 級 checkpoint（`cost-governance-cluster/todo.md`「關鍵事實」已載明），可作為終止後安全恢復點，非本票新建 |
| **session-health 退化** | 串接 `#137` R2 的 `session_health` opaque dict；門檔數值 **`#137` R3 已明文不凍結**，留給消費端（即本票的實作票）依 `#210`（以自身 run 歷史校準）決定 |
| stall／報酬遞減 | issue 引用 MAF Magentic `max_stall` 為類比，**本 repo 查無同義既有機制**（無 `stall`／`diminishing`／`max_turn` 相關治理程式碼），是本票範圍內唯一**全新**需要設計的訊號，本票不預先定義判準（留給實作票，屬「MVP 別做成優化器」同一精神：先有觸發框架，判準可迭代） |
| per-task quota 上限 | 串接 D3 token bucket——`rate_tracker` 若擴充為 per-task（而非只 per-resource）計數，可直接供應此訊號；本票只記錄此依賴方向，不預先擴充 D3 的資料結構（D3 目前只定義 per-resource 桶） |

`should_terminate` 的呼叫端（哪個 daemon 迴圈輪詢它、多久評估一次）本票 MUST NOT
決定——這需要與 executor session 的既有生命週期掛勾點（`launcher.py` 的 wrapper
執行迴圈）對齊，屬未來實作票的範圍，本票只凍結函式簽章與五個觸發來源的資料契約。

風險與緩解：若實作票把「context size 逼近上限」的觸發誤植為已有 hook 可以直接調用，
會在找不到 `precompact` hook 時卡住——緩解：本節明文標注「查無實據」，實作票應先確認
該 hook 是否存在於外部 repo 或本身要新建，不得假設已存在。

## 風險與緩解

- **`filter_ready` 骨架先行，四因子 stub 逐一替換的時序風險**：若某個因子（例如
  `#137` track_record）長期不落地，`judge` 會長期停在「三真一 stub」狀態——緩解：
  D6 已明文 stub 全恆真＝現況等價安全 no-op，不會因任何單一因子延後而阻塞其餘部分
  上線。
- **`rate_tracker.py` 與 `#136` `capacity_gate.py` 邊界混淆**：兩者都是 admission 層
  的「不擋、只排隊/詢問」閘，命名相似（都叫「容量／控速」）容易被誤合併——緩解：D4
  已用架構圖與明文對照表區分兩者管的稀缺資源軸完全不同，code review checklist 可要求
  「新的 rate/quota PR 有沒有誤動 `capacity_gate.py`」作為機械檢查點。
- **429 退避粒度誤用**：`manager_daemon._tick_backoff_seconds` 與本票 per-resource
  退避狀態粒度不同，若實作票圖方便直接 import 該函式並塞入 resource 參數，會污染既有
  daemon-level 純粹語意——緩解：D5 明文「公式重用、狀態不重用」。
- **`context_window`／`quota_window_kind`／`autonomy_safety_profile` 三個 issue 原文
  提及但本票裁定不落地的欄位，未來若證實 MVP judge 真的需要**：緩解路徑已在 D1
  明文——遵循 `#209` R3 先例 additive 擴充 `model-identities.yaml`，不重開檔案。
- **stall／報酬遞減訊號是本票唯一全新概念、無既有先例可抄**：判準設計風險最高的一格
  ——緩解：D7 明文本票不預先定義判準數值，只凍結觸發框架的存在，把判準留給實作票依
  `#210`（run 歷史校準）決定，避免本設計票憑空發明一個未經實測驗證的門檻。
- **「haman/arc + claude/codex 多帳號池」issue 原文描述與 main 現況不符**：若實作票
  照抄 issue 字面去找一個不存在的 config，會卡住——緩解：D4 已明文查證結果（本 repo
  不存在），並保留「池總吞吐＝各桶速率之和」的概念供未來若帳號池真的落地時直接套用
  D3 的 token bucket 模型，不需要另外設計聚合邏輯。

## 與相鄰票的介面關係

- **`#325`（已落地，main，PR `#356`）**：`registry.py` 落地的 job 級 `usage`／
  `usage_raw` 欄位是歷史、per-job、事後的用量記錄，與本票 D3 `rate_tracker` 要的
  即時、per-resource、事前速率閘門是不同資料形狀與更新時機，兩者互補不重疊——`#325`
  issue 本文「非目標」段落自行排除「控速、告警 → #138」，確認邊界不衝突。完整查證見
  `cost-governance-judge-spec.md` R1 段落。
- **`#324`（已落地，main，「combo 可擴充與可選」）**：與本票**無資料或函式介面耦合**，
  屬 workflow/card 派工骨架層（combo 搜尋路徑、`small-fix` 輕量 combo），`#324` issue
  本文「非目標」段落自行畫出邊界（「cost-aware routing → #138」）。記錄查證結果供
  複驗核對，非本票需要接線的相鄰模組。

## 與 `#137` 狀態的訂正說明

本文件初版多處把 `#137`（one-shot 成效閉環／track-record）標注為「已落地設計」，與
`#209`（`design-model-capability-envelope-{spec,design}.md`，main 已落地）並列——
**此標注不成立，已訂正為「設計初稿，`feature/137-oneshot-lesson-loop-design` 分支，
尚未合併 main」**。查證依據：

1. `git ls-tree -r main --name-only | grep -i oneshot` 於本 repo 零命中——main 上不
   存在任何 `#137` 的設計文件。
2. `git merge-base --is-ancestor feature/137-oneshot-lesson-loop-design main` 回傳
   false（未合併）；`git ls-remote --heads origin` 亦無此分支（未推送）。
3. `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的叢集 A 表格本身
   把 `#137` 列為 `open`，與「已落地設計」矛盾。
4. `#209` 自己的 `design-model-capability-envelope-spec.md:98` 在 R1 第 6 項判準把
   `#137` 標注為「尚未落地」——`#209` 對 `#137` 狀態的表述才是準確的，本文件應與其
   一致。

本節訂正不改變本票任何 D/R 決策內容：interim stub 契約（D6／spec R4）本就已把
`track_record` 列為恆真 stub，訂正只是把狀態描述從「已落地設計」精確化為「設計初稿、
未合併」，前述 D2／D3／D4／D6／D7 各處提及 `#137` 之處已同步訂正引用文字。

> **後記（merge 時點更新）**：本節查證反映的是撰寫當下的狀態。`#137` 設計文件其後已隨
> PR #361 合併進 main（`docs/superpowers/specs/oneshot-lesson-loop-{spec,design}.md`），
> 上述「尚未合併」的描述自 PR #361 起不再成立；查證鏈保留作為撰寫過程的如實記錄。

