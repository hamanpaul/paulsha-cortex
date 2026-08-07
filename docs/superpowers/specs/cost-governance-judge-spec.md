---
status: accepted
work_item: cost-governance-judge
---

# cost-governance-judge Specification

#138：成本治理 judge——把 Stage 8「跨三家額度計量器（meter）」升級為
「不擋、只 routing + 控速」的 governance 半邊。凍結 `rate` 自追資料契約、控速分流層
介面契約、429 回授裁定、judge MVP 四因子判斷式、session 終止觸發契約。**本票是設計
文件，不實作 `rate_tracker.py`、不實作控速分流層本體、不改 `autonomy.py`／
`claim_readiness.py`／`manager_daemon.py` 任何一行程式碼。**

## 背景

issue 原文（2026-07-27 起，多則 comment 補充）主張 Stage 8 目前是「強觀測、弱治理」
——`paulshaclaw/cost/`（~1,872 行）做到了跨 vendor quota 計量，但搜不到
budget／limit／alert／throttle／enforce／policy，看得到額度、不做任何控制。本票定義
如何在**不停工**（治理 = routing ＋ 控速，不是 block）的前提下補上治理半邊。

`main` 現況（本票查證，非簡報時點，main @ `a2e8d0c`）：

- 全 repo `grep -rn "token_bucket\|rate_limit"` 於 `paulsha_cortex/` **無治理相關實作
  命中**——「rate 自追」是本票唯一自己負責、且完全空白的因子（cluster todo.md 四因子
  分工表：rate token＝`#138`、quota＝cost meter、戰績＝`#137`、能力＝`#209`）。
- `resource-inventory.yaml`（issue §8.2 指名檔案）**在 repo 中不存在**，且 `#209`
  （已落地設計）D3／R3 已明文裁定 **不新建**此檔，四個能力封套靜態欄位 additive 掛在
  `model-identities.yaml`（schema v2→v3）。本票 D1 沿用此裁定，MVP 不新增第二批靜態
  欄位。
- `capable()`（`#209`，已落地設計——`design-model-capability-envelope-{spec,design}.md`
  於 main 存在，尚未 code-landed）：`claim_readiness.py:18` 與 `:421-437` 明文標註
  `#209 not yet landed`，`capability_probe()` 目前恆
  `_passed("capability", bypass="envelope_unavailable")`——即這格闖關永遠通過。
- `track_record()`（`#137`，**設計初稿、尚未合併 main、尚未 code-landed**）：`git branch
  -a` 顯示 `oneshot-lesson-loop-{spec,design}.md` 只存在於未合併的 sibling 分支
  `feature/137-oneshot-lesson-loop-design`（`git merge-base --is-ancestor` 對 `main`
  回傳 false；`git ls-remote --heads origin` 亦無此分支），main 上 `git ls-tree -r main
  --name-only | grep oneshot` 零命中——**不可與 `#209`／`#139` 的「已落地設計」等同視之**。
  函式簽章 `track_record(resource, task_type: str, scope: str | None = None) -> float`
  引自該分支草稿 R4（與 `#209` R1 第六項判準相容），本票只引用其簽章作為介面對齊依據，
  不代表該設計已定案落地；下文所有「`#137` 已落地設計」字樣統一訂正為此處的準確狀態，
  詳見「與 #137 狀態的訂正說明」段落。
- `#136`（已落地，code + design）：`paulsha_cortex/porcelain/capacity_gate.py`
  `evaluate_gate()` 是 **PreToolUse hook 用的 daemon-idle 布林閘**，管的是互動 session
  中手動 spawn subagent/headless 的破口，與本票要的「resource 級 quota/rate 是否有
  餘裕」是不同稀缺資源軸的並行閘，非同一機制的擴充（見 design D4）。
- `#139`（已落地設計）R7 已凍結 status view 四鍵契約（`quota`／`rate`／`health`／
  `track_record`）；本票只交付 `rate` 這一格的資料契約，其餘三格分屬 cost meter／
  `#139`／`#137`。
- `manager_daemon.py:210` `_tick_backoff_seconds()` 是 **daemon tick 迴圈**自身的
  退避（單一 `consecutive_tick_failures` 計數器），粒度上不是「每個 resource 各自的
  請求速率退避」，公式模式可重用，狀態不可重用（見 design D5）。
- `autonomy.py:394` `ready_units()` 與 `autonomy.py:447` `dispatch_ready()` 之間目前
  **沒有任何** admission/routing 過濾層——`ready_units()` 判斷結構完整性後直接交給
  `dispatch_ready()` fan-out，這正是本票 D4 要插入「控速分流層」的空隙，且此掛點選擇
  與同批 `#137` design 草稿（未合併分支）D4 的建議掛點一致（該草稿主張「棘輪不是
  `autonomy.py` 內部新函式，而是 `#209 capable()` 的一個因子，被 `#138` judge
  呼叫」）——僅作為介面對齊參考，不代表該草稿已定案或已合併。
- `#324`（已落地，main，「combo 可擴充與可選」）與本票**無資料或函式介面耦合**：
  `#324` 落地的是 `deck/schema.py` 的 combo 搜尋路徑（instance-local override）與
  `small-fix` 輕量 combo，屬 workflow/card 派工骨架層；`#324` issue 原文「非目標」
  段落自行畫出邊界——「勝率／outcome scoring → `#137`；cost-aware routing →
  `#138`」，即 `#324` 明文把本票的範圍排除在外。本票在此記錄查證結果：兩者是
  **不相交**的責任邊界（combo 選牌 vs. resource 級 cost-aware routing），非「有
  介面待對齊」關係，不需要本票額外定義接線點。

## 與 `#137` 狀態的訂正說明

本文件初版曾多處把 `#137`（one-shot 成效閉環／track-record）標注為「已落地設計」，
與 `#209`（`design-model-capability-envelope-{spec,design}.md`）、`#139`
（`design-task-type-taxonomy-v2-{spec,design}.md`）並列——**此標注不成立，已訂正**。
複驗查證（`main @ a2e8d0c`）：

- `git ls-tree -r main --name-only | grep -i oneshot`：零命中，main 上不存在任何
  `#137` 的設計文件。
- `#137` 的 `oneshot-lesson-loop-{spec,design}.md` 只存在於本 repo 本機的 sibling
  分支 `feature/137-oneshot-lesson-loop-design`（`git merge-base --is-ancestor
  feature/137-oneshot-lesson-loop-design main` 回傳 false）；`git ls-remote --heads
  origin` 亦無此分支，未推送到遠端，更未合併。
- `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 自身的叢集 A 表格
  （本檔緊鄰列）把 `#137` 標為 **`open`**，與「已落地設計」矛盾。
- `#209` 自己的 `design-model-capability-envelope-spec.md:98` 在 R1 第 6 項判準把
  `#137` 標注為「`#137`（**尚未落地**；`grep -rn "track_record" paulsha_cortex`
  零命中，本票只引用函式簽章，不假設其內部實作）」——`#209` 才是對 `#137` 狀態的
  準確表述，本文件應與其一致而非自相矛盾。

**修正後的準確表述**：`#137` 的設計是**未合併的本機分支草稿**，非「已落地設計」。
本文件下游所有引用 `#137` 函式簽章／掛點建議之處，語意皆改為「引用該草稿內容作為
介面對齊參考，不代表其已定案、已合併或已落地」；`#137` 本體（設計與程式碼）皆待該
分支正式開 PR 並合併 main 後才算落地。這不影響本票（`#138`）的任何 D/R 決策內容
本身——interim stub 契約（R4）本就已把 `track_record` 列為恆真 stub，訂正只是狀態
描述用語精確化，不變更設計。

> **後記（merge 時點更新）**：本節查證反映的是撰寫當下的狀態。`#137` 設計文件其後已隨
> PR #361 合併進 main（`docs/superpowers/specs/oneshot-lesson-loop-{spec,design}.md`），
> 上述「尚未合併」的描述自 PR #361 起不再成立；查證鏈保留作為撰寫過程的如實記錄。

## Goals

- 凍結 `rate` 自追（token bucket）資料契約，作為 `#139` R7 status view `rate` 鍵的
  資料源。
- 凍結控速分流層 `filter_ready()` 介面契約，明確掛點為 `autonomy.py` `ready_units()`
  與 `dispatch_ready()` 之間的新過濾步驟，並與 `#136` 既有 `capacity_gate.py` 劃清
  「不同稀缺資源軸」的邊界。
- 凍結 429 回授裁定：退避公式重用 `manager_daemon._tick_backoff_seconds` 的指數封頂
  模式，退避狀態不重用（per-resource 獨立）。
- 凍結 judge MVP 四因子合取判斷式，並明定四因子在對應設計票（`#137`／`#209`）尚未
  code-landed 期間的 interim stub 契約——stub 全恆真時行為與現況等價（安全 no-op）。
- 凍結 session 終止觸發契約（`should_terminate`），串接 `#137` R2 的 `session_health`
  opaque pass-through 邊界，五個觸發來源逐一標註 main 現況（含「查無實據」的誠實記錄）。
- 明載非目標：不新增 `resource-inventory.yaml`／不新增 `model-identities.yaml` 第二批
  靜態欄位、不實作任何程式碼、不改 `#136`／`#137`／`#209` 既有文字、不開
  `openspec/changes/**`。

## Requirements

### R1 `rate` 自追資料契約凍結

`rate` 狀態 SHALL 為鍵 `f"{executor}:{model_id}"`（複合鍵字串化，底層 `(executor,
model_id)` 與 `#209` R2 一致）到 `RateSnapshot` 的映射，`RateSnapshot` 欄位：

| 欄位 | 型別 | 語意 |
|---|---|---|
| `available` | `bool` | MVP judge（R4）唯一實際消費的欄位 |
| `tokens_remaining` | `float \| None` | token bucket 餘量，reserved，`None`＝尚未觀測 |
| `window_seconds` | `float \| None` | bucket 補充週期，reserved |
| `last_429_at` | `str \| None`（ISO8601） | 供 R3 429 回授判讀 |

`rate_tracker` 模組（實作票落點：新檔 `paulsha_cortex/coordinator/rate_tracker.py`）
SHALL 提供純函式 `consume(executor: str, model_id: str) -> bool`（惰性補充後嘗試扣一
token，回傳是否可送）與 `record_429(executor: str, model_id: str) -> None`（收縮
`capacity`、記錄 `last_429_at`）。token bucket 內部狀態 MUST NOT 要求跨 daemon 重啟
持久化（冷啟動重新學習是可接受降級，MUST NOT 比照 `registry.py` job record 等級的
durable JSON persistence 設計）。

`rate_tracker` MUST NOT 擴充 `model_identities.py`（靜態 registry）、
`claim_readiness.py`（一次性交易）、或 `manager_daemon.py`（daemon-level 迴圈狀態）
三者任一——三者的既有語意邊界理由見 design D3。

**與 `#325`（已落地，main @ PR `#356`）的介面關係**：`#325`
（「job record 收斂 token usage」）已在 `registry.py` 落地 job 級的 `usage`／
`usage_raw` 欄位（`input_tokens`／`output_tokens`／`cached_input_tokens`／
`reasoning_output_tokens`，per-executor adapter 抽取），是**歷史、per-job、事後**
的用量歸屬記錄，供 `cortex jobs`/`stat` 輸出消費。`rate_tracker`（本節）是
**即時、per-resource、事前**的請求速率閘門（token bucket 的 `available` 布林），
兩者資料形狀與更新時機都不同，不是同一份資料的兩種投影——`rate_tracker` MUST NOT
直接消費 `usage`/`usage_raw` 作為即時判斷依據（`#325` 的 issue 本文「非目標」段落
自身也明文排除「不做預算、擋工、控速、告警 → #138」，確認兩者是互補而非重疊的
分工）。若未來要用 `#325` 累積的歷史 usage 校準 `rate_tracker` 的 `capacity`
初始值或做離線分析，屬後續實作票範圍，本票只記錄此依賴方向、不預先設計。

### R2 控速分流層介面契約凍結，掛點為 `autonomy.py` 兩函式之間

新函式契約（實作票落點：新模組，命名與檔案位置留給實作票，不預先綁定）：

```python
def filter_ready(
    units: Sequence[ReadyUnit],
    judge: Callable[[ReadyUnit], JudgeResult],
) -> tuple[Sequence[ReadyUnit], Sequence[QueuedUnit]]:
    ...
```

`filter_ready` SHALL 插在 `autonomy.ready_units()`（`autonomy.py:394`）呼叫端與
`autonomy.dispatch_ready()`（`autonomy.py:447`）呼叫端之間，屬 manager tick 迴圈
（`manager_daemon.py`）新增的一個步驟，MUST NOT 修改 `ready_units()` 或
`dispatch_ready()` 既有簽章。`filter_ready` 只做「現在派 vs 先排隊」的二分，MUST NOT
承擔 eligibility（該不該派）判斷——那已在 `ready_units()` 之前由 `#208` sizing／
`#209` capable() 的 eligibility 閘處理完畢。`QueuedUnit` 只是延後到下一輪 tick 重新
評估，MUST NOT 寫入任何 terminal 狀態（不得標記為 failed/needs_human）。

`filter_ready` 與 `#136` `capacity_gate.evaluate_gate()` 是**並行的兩把閘**，
MUST NOT 合併實作或互相呼叫——`capacity_gate` 判 daemon 忙不忙（單一布林），
`filter_ready` 判各 resource 的 quota/rate 是否有餘裕（per-resource），兩者是 admission
層（`#209` R4 表格）上不同稀缺資源軸的獨立判準。

### R3 429 回授：公式重用，狀態不重用

`rate_tracker.record_429()`（R1）SHALL 重用 `manager_daemon._tick_backoff_seconds()`
（`manager_daemon.py:210`）的**指數成長＋封頂公式結構**（`base * 2**min(exponent,
cap)`），MUST NOT 直接呼叫或依賴該函式本身、MUST NOT 與 daemon-level
`consecutive_tick_failures` 共用同一個計數器——429 退避狀態 SHALL 為 per
`(executor, model_id)` 獨立追蹤。

Telegram/bro 側既有 backoff（issue §7 提及）本票 MUST NOT 假設其介面存在或杜撰其
路徑——`bro` 不在本 repo，本票查無實據。後續實作票 SHALL 優先採用本節裁定的
`manager_daemon` 公式模式；若要重用 bro 側 pattern，MUST 先向該 repo owner 確認介面
存在，不得憑空假設。

### R4 judge MVP 四因子判斷式凍結

```
judge(work, resource) =
      rate_available(resource)                            -- R1，本票
    ∧ quota_remaining(resource) > 0                        -- cost meter（外部）
    ∧ capable(resource, work)                              -- #209 R1（六項合取）
    ∧ track_record(resource, work.task_type) ≥ threshold   -- #137 R4
```

`judge` SHALL 選第一個四項皆真的 resource（deterministic 迭代順序），MUST NOT 做加權
評分或優化排序（比照 `#209` D1、cluster todo.md「已定案」第 5 條）。

**interim stub 契約（MUST 遵循）**：四因子中任一因子的對應設計票尚未 code-landed
時，`filter_ready`（R2）的實作 SHALL 對該因子使用恆真 stub，且此狀態 MUST 視為與
「`ready_units()` 之後無任何過濾、直接 `dispatch_ready()`」的現況行為等價（安全
no-op）。四個 stub 的真值替換點：

| 因子 | stub 替換為真值的前置條件 |
|---|---|
| `rate_available` | `rate_tracker.consume()` 落地（R1 實作票） |
| `quota_remaining` | cost meter 接進 status view（`#139` R7，未來票） |
| `capable` | `#209` R1 的 `capable()` 本體落地 |
| `track_record` | `#137` R4 的 `track_record()` 本體落地 |

`filter_ready` 骨架 MUST NOT 因任一因子的對應票延後而被阻擋上線——四者可獨立分批
替換。

### R5 session 終止觸發契約凍結

```python
def should_terminate(signals: SessionSignals) -> TerminationDecision | None:
    ...
```

任一觸發條件成立即回傳非 `None` 的終止決策，全不成立回 `None`。觸發來源 SHALL 涵蓋
以下五類，且 `session-health` 一項 MUST 遵循 `#137` R2 既有邊界（`session_health` 為
`dict | None` opaque pass-through，MUST NOT 併入任何 reward/outcome 計分，只可用於
早期預警／終止判斷）：

1. context size 逼近上限（本票查無現成 hook，見背景段「precompact」查證）。
2. checkpoint（重用既有 `manager_daemon.py`／`claim.py` phase 級 checkpoint 作為安全
   恢復點）。
3. session-health 退化（`#137` `session_health`，門檻數值 `#137` R3 已明文不凍結，
   留給本票未來實作票依 `#210` 校準）。
4. stall／報酬遞減（本 repo 無既有同義機制，本票不預先定義判準數值）。
5. per-task quota 上限（依賴 R1 token bucket 若未來擴充為 per-task 計數；本票不預先
   擴充 R1 的資料結構）。

`should_terminate` 的呼叫端輪詢頻率與掛勾的 executor session 生命週期位置（例如
`launcher.py` wrapper 執行迴圈）本票 MUST NOT 決定，留給未來實作票對齊既有生命週期
掛勾點。

### R6 靜態欄位裁定：MVP 不新增，未來如需新增遵循 `#209` R3 先例

MVP judge（R4）不需要 `context_window`／`quota_window_kind`／`autonomy_safety_profile`
三個 issue §3 原文提及的靜態欄位。若未來票證實需要，SHALL additive 擴充既有
`model-identities.yaml`（`#209` R3 既定路徑），MUST NOT 新建
`resource-inventory.yaml` 或任何第二個 inventory 檔案，MUST NOT 與 `#209` 既有四欄位
（`accepts_bands`／`invariant_ceiling`／`consistency_scope`／`acceptance_modes`）撞名。

### R7 `rate` 鍵回填 `#139` status view 契約，不另開查詢介面

`rate_tracker` SHALL 提供 `rate_status(executor: str, model_id: str) ->
RateSnapshot`，作為 `#139`（`design-task-type-taxonomy-v2-spec.md` R7）status view
動態 JOIN 的 `rate` 鍵資料源，與 `quota`／`health`／`track_record` 三格並列被同一個
JOIN 呼叫端讀取。`rate_tracker` MUST NOT 自建獨立 CLI 或另一條查詢路徑與其餘三格
分裂。

## 非目標

- 不實作 `rate_tracker.py`、`filter_ready()`、`should_terminate()` 任一本體。
- 不接線 cost meter（`paulshaclaw/cost/`）進 status view `quota` 鍵——那是待未來票對齊
  `paulshaclaw` owner 的跨 repo工作，本票只記錄依賴方向。
- 不實作 `#209` `capable()`、不實作 `#137` `track_record()`——本票只引用 `#209`
  已凍結（main 落地）的函式簽章，以及 `#137` 未合併草稿分支提出、尚未定案的函式簽章
  （見「與 `#137` 狀態的訂正說明」）。
- 不修改 `autonomy.py`／`claim_readiness.py`／`manager_daemon.py`／
  `capacity_gate.py` 既有程式碼一行。
- 不修改 `#136`／`#137`／`#209` 既有設計文件文字。
- 不新增 `model-identities.yaml` 第二批靜態欄位（R6）、不新建
  `resource-inventory.yaml`。
- 不新增 `openspec/changes/**` 底下的 change 目錄——issue 原文明文「落地時再依
  OpenSpec 流程開 change」。
- 不定義 stall／報酬遞減、session-health 退化兩項終止觸發的具體數值門檻（R5 已明文
  留給實作票）。
- 不決定 `weight(work)`／`headroom(resource)` 是否為單一標量（cluster todo.md 未決
  事項 #2 原樣保留）。

## 驗收面

- `cost-governance-judge-spec.md`／`cost-governance-judge-design.md` 皆含非空
  frontmatter（`status`／`work_item`）與非空 Requirements／Decisions 段落，行數量級
  比照 `design-model-capability-envelope-{spec,design}.md`（256／227 行，main 已落地
  可直接核對）；`oneshot-lesson-loop-{spec,design}.md`（195／158 行）僅存在於 `#137`
  未合併的 `feature/137-oneshot-lesson-loop-design` 分支，**於 main checkout 上核對
  此條會找不到檔案**——複驗者須先 `git fetch`／切到該分支才能核對，或視為暫不可驗證。
- R1 `RateSnapshot` 欄位與 `#209` R2 的 `(executor, model_id)` 複合鍵一致（可用
  `grep -n "(executor, model_id)"` 於 `design-model-capability-envelope-spec.md`
  與本文件互相核對，兩者皆在 main，可直接驗證）。
- R4 的四因子判斷式：`capable()` 簽章可在 main 核對，見
  `design-model-capability-envelope-spec.md:98`；`track_record()` 簽章僅能在
  `#137` 未合併分支的 `oneshot-lesson-loop-spec.md:150` 核對，main 上暫不可驗證
  （見「與 `#137` 狀態的訂正說明」）。
- R2 的 `filter_ready` 掛點（`autonomy.py:394` 與 `:447` 之間）與 `#137`
  未合併草稿 `oneshot-lesson-loop-design.md` D4 的建議掛點描述一致（互相印證，非本票
  孤立主張；但該草稿本身尚未合併 main，此條驗證同樣需先取得該分支）。
- `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的 `#138` 列狀態
  更新為「設計文件已交付」。
- 全套 `pytest` 維持綠燈不倒退（本票不改 `.py`，屬留證性質而非風險緩解）。
