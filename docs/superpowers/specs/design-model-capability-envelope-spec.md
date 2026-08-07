---
status: accepted
work_item: design-model-capability-envelope
---

# design-model-capability-envelope Specification

#209：定案 `#138` judge 公式裡唯一無內容的謂詞「能力配得上」——`capable()` 六項判準與
`resource-inventory.yaml`（供 `#139`）四個靜態欄位契約，並定案 topic×sizing band 路由矩陣
與 `#136`／`#138`／`#209` 三閘（eligibility／admission／routing）邊界。**本票是設計文件，不
實作 `capable()`、不改任何 registry schema、不實作 judge。**

## 背景

`#138` 已議定 judge 公式：`指派規則 = 有 rate token × quota 有餘 × 能力配得上 × 這類戰績好 →
選一個資源`。四因子中「有 rate token」（`#138`）、「quota 有餘」（cost meter）、「這類戰績好」
（`#137` track-record）皆有歸屬，唯獨「能力配得上」未定義——這正是本票的範圍。

需求側量表已由 `#208` 落地並在 main 生產可用：

- `paulsha_cortex/coordinator/claim.py:1098` `sizing_band()`：五維總分（0–10）→
  `green`（≤3）／`yellow`（≤6）／`red`（>6），沿用 `deck.schema.BAND_LEVELS`。
- `paulsha_cortex/coordinator/planning.py:462-472` 已消費 plan frontmatter 的
  `invariant_count`／`artifact_classes` 兩個宣告欄位（`#212`）。
- `paulsha_cortex/coordinator/work_bridge.py:402-424`、`registry.py:1229,1291,1346,1425`、
  `manager.py:6083,6106,7446`、`delivery.py:38-59`（`REPAIR_BUDGET_BY_BAND`）、
  `completion.py:268-379` 皆已消費 `sizing_band`／`sizing_score`。

供給側完全空白，且**現況比 issue 原文自己以為的更空**：

- `paulsha_cortex/coordinator/claim_readiness.py:18` 明文標註
  `5. capability — capable() predicate table lookup (config; #209 not yet landed, ...)`；
  `claim_readiness.py:421-437` 的 `capability_probe()` 在無 lookup provider 時一律
  `_passed("capability", bypass="envelope_unavailable")`——即目前這格闖關永遠通過，不做任何過濾。
- `paulsha_cortex/coordinator/planning.py:456-509` 的 `_plan_review_envelope()` 同樣有
  `envelope_lookup` 掛勾，但 `manager.py:5988` 目前固定傳入 `envelope_lookup=None`，恆走
  bypass 分支（見 D7）。
- `paulsha_cortex/coordinator/data/model-identities.yaml`（packaged registry，repo 內
  唯一有 loader／fail-closed 驗證、被 `model_identities.py` 實際讀取的身分清單）
  **全文只有一個身分**：

  ```yaml
  schema_version: 2
  identities:
    - executor: agy
      model_id: gemini-3.1-pro-high
      independence_domain: google
      capabilities: [planning]
      live_probe: agy-plan-sandbox
  ```

  即 `capabilities` 唯一值是 `planning`；packaged registry 內**沒有任何身分**帶 `build`
  或 `review` capability。issue 原文 §4.1 初版的三身分表（`codex/gpt-5.3-codex-spark`、
  `copilot/gpt-5.4`、`claude/sonnet`、`Luna(Opus max)`）與其 2026-07-27 comment 的
  **修正表**（`agy`/`claude-sonnet-4-6`〔build〕、`agy`/`gemini-3.6-flash-high`〔review〕、
  `agy`/`Gemini 3.1 Pro (High)`〔planning〕）皆與 packaged registry 現況不符。但這兩個
  model_id 在全 repo grep **並非只存在於 issue comment**——
  `docs/superpowers/workstreams/cost-governance-cluster/todo.md:129` 是本 repo 既有、
  受版控的「關鍵事實（避免重犯）」筆記，重申幾乎逐字相同的三身分表；
  `driving-cortex-skill/todo.md:12` 另有 1 處提及 `gemini-3.6-flash-high`。本文件的 §4
  更正以「packaged registry（唯一有 loader 的檔案）現況只有 1 身分」為準，但明確記錄
  `todo.md:129` 與此矛盾、尚未收斂（見「現況更正」節 R6 與
  `docs/superpowers/specs/design-model-capability-envelope-design.md` D6）——這牽涉另一份
  獨立的受版控文件，需要與其 owner 對齊，非本票單方面可判定孰是孰非。
- `resource-inventory.yaml`（issue §8.2 指名的落地檔案）**在 repo 中不存在**；issue 原文
  §139 引用該檔名owner 為 `#139`，但 `#139`（issue body 已核對）本身的落地清單是「session log
  reader／`project_resolver` 歸屬／resource status view（動態 JOIN）／outcome ledger／
  session-health 訊號源」六項，並未含一個具體命名的 `resource-inventory.yaml` 靜態檔。截至
  main @ `9bda3c0`，`#139` 的 taxonomy 子項已透過 `#202`／`task-types.yaml` 落地，但
  resource-inventory 本身仍是設計層面的懸置命名，尚未有任何 PR 建立它。

## Goals

- 定案 `capable()` 六項合取式判準，逐項標註來源與型別，供 `#138` judge 消費。
- 定案供給側四個新靜態欄位（`accepts_bands`／`invariant_ceiling`／`consistency_scope`／
  `acceptance_modes`）的型別／值域／複合鍵，並解決「落地在哪個檔案」這個 issue 原文未答的
  懸置問題（見 R3）。
- 定案 topic×sizing band 路由矩陣在現行 roster（僅 1 個 `build` 身分）下的實際語意邊界。
- 對齊 `#136`／`#138`／`#209` 的三閘序（eligibility／admission／routing）與既有
  `claim_readiness.CHECK_ORDER` 的落差。
- 更正 issue §4 的 roster 現況描述，並記錄本票查證到「issue 自身修正也未反映 main」的更深一層落差。
- 明載既有消費端契約點（`planning._plan_review_envelope`／`plan_review_gate`）的既定介面形狀，
  後續實作票必須沿用而非另起爐灶。

## Requirements

### R1 `capable()` 六項合取式契約凍結

`capable(resource, work)` SHALL 為以下六項的合取（AND），任一項為否即整體為否：

| # | 項目 | 表達式 | 來源 | 型別 |
|---|---|---|---|---|
| 1 | sizing band | `work.sizing_band ∈ resource.accepts_bands` | `#208`（`claim.sizing_band()`，`claim.py:1098`） | `str ∈ set[str]` |
| 2 | invariant 上限 | `work.invariant_count ≤ resource.invariant_ceiling` | 本票新定義（欄位已被 `planning.py:462` 消費，但目前只跟「plan 自己宣告的封套」比，不是跟 resource registry 比） | `int ≤ int` |
| 3 | 一致性半徑 | `work.artifact_classes ⊆ resource.consistency_scope` | 本票新定義（`artifact_classes` 集合語意沿用 `planning.py:465-472` 既有實作） | `set[str] ⊆ set[str]` |
| 4 | 驗收模式 | `work.acceptance_mode ∈ resource.acceptance_modes` | 本票新定義 | `str ∈ set[str]` |
| 5 | capability 子集 | `work.required_capabilities ⊆ resource.capabilities` | 已落地（`#130`，`model_identities.py` `ModelIdentity.capabilities`） | `set[str] ⊆ set[str]` |
| 6 | track record | `track_record(resource, work.task_type) ≥ threshold` | `#137`（尚未落地；`grep -rn "track_record" paulsha_cortex` 零命中，本票只引用函式簽章，不假設其內部實作） | `Callable[[Resource, str], float] ≥ float` |

`capable()` 的實作 SHALL 為純函式（無 I/O），供 `#138` judge 呼叫；六項判準 MUST 全部評估完
（不得因單項為否而跳過其餘五項的可觀測性紀錄），以支援 issue §9 驗收條件「`cortex stat`
可顯示每次指派的 `capable()` 判定依據與被排除的身分原因」。本票只定簽章與判準集合，不寫
`capable()` 本體。

### R2 `resource-inventory` 四個靜態欄位型別／值域契約

| 欄位 | 型別 | 值域 | 空值語意 |
|---|---|---|---|
| `accepts_bands` | `list[str]` | `green`／`yellow`／`red` 子集，非空 | 缺省 MUST 拒載（無法判斷 R1 項 1） |
| `invariant_ceiling` | `int` | `≥ 0` | 缺省 SHALL 視為 `capability` 檢查 bypass（比照 `claim_readiness.capability_probe` 現行 `envelope_unavailable` 語意），MUST NOT 視為 `0`（會誤傷一切） |
| `consistency_scope` | `list[str]` | `code`／`test`／`spec`／`openspec`／`changelog`／`docs`／`pr`／`issue` 子集，非空 | 同上，bypass 而非空集合 |
| `acceptance_modes` | `list[str]` | `focused_tests`／`repo_gate`／`live_evidence`／`github_closure` 子集，非空 | 同上，bypass 而非空集合 |

四欄位 SHALL 掛在複合鍵 `(executor, model_id)` 上——與 `model_identities.py:159-160`
（`IdentityRegistry.get`）既有的去重鍵完全一致，理由是本 repo 的 `executor` 是 launcher
（`agy`／未來的 `claude`／`codex`），不是 vendor；同一 `executor` 下可有多個 `model_id`
（見 `model_identities.py` 註解 `AGY_MODEL_ID` 一段）。四欄位 MUST NOT 掛在單獨的
`executor` 或單獨的 `model_id` 上。

任一欄位載入時型別或值域錯誤 MUST fail-closed 拒載，比照 `model_identities.py:115-152`
（`IdentityRegistry.from_rows`）既有的 `allowed` 白名單與逐欄驗證慣例。

### R3 落地位置：短期併入 `model-identities.yaml`，不新建 `resource-inventory.yaml`

四個新欄位的實作落地 SHALL 優先擴充既有 `paulsha_cortex/coordinator/data/model-identities.yaml`
／`model_identities.py`（`IdentityRegistry`，schema 由 `MODEL_IDENTITY_SCHEMA_VERSION = 2`
升版），MUST NOT 在後續實作票裡新建一個不存在 owner、不存在 schema 的
`resource-inventory.yaml` 空降檔案。

理由：issue §8.2 寫「供 `#139`」的 `resource-inventory.yaml`，但 `#139` 的 issue body（本票
已核對）並未把「新增一個靜態 config 檔」列在其落地任務清單中——`#139` 的六項共用基礎設施皆是
「讀取層」或「動態 JOIN」（session log reader、`project_resolver`、resource status view、
outcome ledger、session-health），沒有一項是「新增一份靜態 inventory 檔案」的所有權宣告。
`model-identities.yaml` 才是 repo 內唯一已存在、已有 loader（`model_identities.py`）、已有
fail-closed 驗證慣例、且複合鍵語意（`executor`/`model_id`）與本票 R2 完全吻合的落地位置。若
`#139` 未來真的定案獨立的 `resource-inventory.yaml`（例如要把 capability envelope 之外的
quota／health 也一起收），欄位遷移 MUST 為 additive-only 搬遷（不改欄位名／值域語意），並由
`#139` 的實作票自行決定，不在本票預先假設。

### R4 三閘序（eligibility／admission／routing）契約凍結

`#136`／`#138`／`#209` 的邊界依 2026-07-27 議定（issue #209 §9.5，並與
`docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的「已定案」第 5 條一致
——**注意**：本票引用的是該 todo.md 的三閘序裁決（第 5 條），與同一份 todo.md 第 129 行
「registry 實際三個身分」的筆記是不同段落、不同性質的陳述；後者與 packaged registry 現況
矛盾，見 R6，本票不因為第 5 條可信就連帶背書第 129 行）
凍結為：

| 閘 | 回答的問題 | 擋不擋 | 判準 | 落在 |
|---|---|---|---|---|
| eligibility | 該不該派 | **擋**（終局） | 失敗是否可自癒＝否 | `#208` sizing + 本票 envelope（R1／R2） |
| admission | 現在派得動嗎 | **不擋**，排隊＋控速 | 失敗是否可自癒＝是 | `#136`（容量）＋`#138`（額度／rate） |
| routing | 派給誰 | 選一個資源 | — | `#138` judge ＋ 本票 `capable()`（R1） |

閘序 SHALL 為 `sizing/envelope → preflight → capacity/quota → judge/routing`；容量閘
MUST NOT 排第一（理由見 issue §9.5：Red work 若先排隊等資源，最壞情況是等了資源又派出一個
注定重跑的工作）。

**現況落差（本票查證，issue 原文未提及）**：`claim_readiness.py:57-64` 的
`CHECK_ORDER = (local_scope, base_sha, monitor_snapshot, github_owner, capability, live_probe)`
把 R1（eligibility 性質的 capability 檢查）與 `live_probe`（實際啟動一個 session，性質上更接近
admission／routing 之後才該付的成本）放在**同一條線性 fail-closed/retryable 分類**的交易裡，
尚未真正實作「擋 vs 排隊」的語意分岔——目前 `capability` 檢查若失敗，走的是
`claim_readiness.ReadinessOutcome` 既有的 terminal／retryable 二分類（terminal 僅限
policy-scope-conflict），不是 R4 表格裡「排隊」的第三種結局。後續實作票 MUST 決定：
是在 `CHECK_ORDER` 內新增「排隊」結局類型，還是在 `CHECK_ORDER` 之外另立 admission 層；本票
不預先決定，只記錄這個現存的架構落差供實作票評估。

### R5 topic×sizing band 路由矩陣現況語意限制

issue §5 的路由矩陣（依 topic 家族分派 band／不變量特性／路由策略）SHALL 保留為 §9.5
eligibility 判準的參考輸入，但 MUST 明確標註：**在 registry 只有一個具 `build` capability
身分的現況下（R2 現況更正一節），矩陣只有「該不該派」（eligibility）語意，沒有「派給誰」
（routing）語意**——因為沒有第二個 builder 可選。矩陣中 `red` 帶（`fix(workflow)`／
`fix(coordinator)`）「不可單一 build 身分 end-to-end」的結論不受此限制影響，因為那是
eligibility 判斷（該不該直接派）而非 routing 判斷。roster 擴充後（新增第二個 `build`
capability 身分）矩陣才會產生實際的 routing 分流效果；本票不假設擴充時程。

### R6 registry 現況更正（packaged registry 僅 1 身分；與 todo.md:129 的矛盾待收斂）

本票文件 MUST 明載以下事實，取代 issue §4.1 的任何版本（含其 2026-07-27 comment 的修正表）：

- packaged registry（`paulsha_cortex/coordinator/data/model-identities.yaml`，repo 內
  唯一有 loader／fail-closed 驗證的身分清單）現況**只有一個身分**：`agy` /
  `gemini-3.1-pro-high` / `independence_domain: google` / `capabilities: [planning]` /
  `live_probe: agy-plan-sandbox`。
- 全 repo grep `claude-sonnet-4-6`、`gemini-3.6-flash-high` **並非零命中**：issue 自身
  2026-07-27 comment 宣稱的「registry 實際三個身分」表與 packaged registry 不符，但
  `docs/superpowers/workstreams/cost-governance-cluster/todo.md:129`（受版控、屬本 repo
  「關鍵事實」筆記）重申幾乎逐字相同的三身分表；`driving-cortex-skill/todo.md:12` 另有 1
  處提及 `gemini-3.6-flash-high`；`tests/test_model_identities.py:368,376` 為測試 fixture
  字面巧合，與 registry 宣告無關。本票以 packaged registry（唯一有 loader 的檔案）現況為
  R1–R8 的資料依據，但 `todo.md:129` 與此矛盾**尚未收斂**——本票不擅自判定 `todo.md:129`
  是過時筆記還是反映某個 host-local overlay（`model_identities.py` 允許
  `$PSC_PROJECT_CONFIG_ROOT` 疊加，理論上可能），這需要與 `cost-governance-cluster` todo
  owner 對齊，超出本票（design-model-capability-envelope）的範圍。詳細記錄見
  `docs/superpowers/specs/design-model-capability-envelope-design.md` D6。
- 因此**packaged registry 目前唯一登錄的身分完全不具備 `build` 或 `review` capability**；
  R1 項 5（`required_capabilities ⊆ resource.capabilities`）在絕大多數 build/review 型工作
  上會對這個唯一身分判否——`capable()` 落地後，若沒有同時擴充 registry 補上至少一個 `build`
  身分，現行 build 派工會從「無過濾（bypass）」變成「全部被 `capable()` 擋下」；若
  `todo.md:129` 的三身分表確實反映某個已生效的 overlay，這個風險評估的前提本身需要重新
  核實。這是後續實作票上線前 MUST 先確認的環境前提，不是本票要解決的範圍。

### R7 既有消費端契約點必須被沿用

`paulsha_cortex/coordinator/planning.py:456-509`（`_plan_review_envelope`）與
`planning.py:515-545` 附近的 `plan_review_gate(..., envelope_lookup=...)` 已經是 `#212`
（`#208` 拆分子單）落地的既定介面：`envelope_lookup: Callable[[], Mapping[str, object] | None]`，
回傳值 MUST 含 `invariant_count: int` 與 `artifact_classes: list[str]` 兩鍵；
`manager.py:5988` 現況固定傳入 `envelope_lookup=None`（bypass，不查表）。

後續實作 `capable()` 供給端時 SHALL 提供一個與此形狀相容的 provider（可以是同一份查表函式的
不同投影），MUST NOT 修改 `_plan_review_envelope` 既有兩鍵的語意；新增欄位（`accepts_bands`
等）如需傳入 plan review 這一層，MUST 以 additive 方式擴充 `envelope_lookup` 回傳的 Mapping，
不得移除或改名既有兩鍵。

### R8 命名消歧：`acceptance_mode` 與既有 `acceptance_surfaces`

repo 內已存在 `acceptance_surfaces`（`planning.py:591,618,668-696`；`manager.py:5935,5982-5988`）
——這是 `#208`/`#221` H.1 五維 sizing 分數的其中一維，型別為 **`int`**（gate_spine 核心層計數
與適用規則數的組合訊號，三級門檻），與 `PlanningQuestion`／`plan_review_gate` 的
`acceptance_surfaces: frozenset[str]` 參數（拿來對 Tasks 逐項核對的**集合**）同名異義已經
在既有程式碼中並存。

issue §8.1／§8.2 新增的 `acceptance_mode`（work profile 單值枚舉，descriptive of *how* a
work item will be verified：`focused_tests`／`repo_gate`／`live_evidence`／
`github_closure`）與 `acceptance_modes`（resource 靜態欄位，同一詞彙的集合）在拼字上與既有
`acceptance_surfaces` 高度相似但語意完全不同（一個是「驗收手段」、一個是「產物/規則覆蓋數」）。
本票 MUST 明載此區別，後續實作票 MUST NOT 把 `acceptance_mode` 誤植為
`acceptance_surfaces` 的別名或反之；變數命名與 docstring MUST 保留兩者的既有全名，不得縮寫
成容易混淆的形式（如同時出現 `acceptance_surf` 與 `acceptance_mode` 縮寫）。

## 非目標

- 不實作 `capable()` 本體、不改 `model_identities.py`／`claim_readiness.py` 任何一行程式碼。
- 不新增 `resource-inventory.yaml` 實體檔案（R3 已論證其 owner 與時程未定）。
- 不做成優化器；指派規則維持 `#138` 議定的 MVP 形態（滿足條件即選一個資源）。
- 不決定 `weight(work)`／`headroom(resource)` 是否為單一標量（issue §9.5「未收斂」原樣保留，
  留給 `#136`／`#138` 實作票）。
- 不以本票否定任何模型；封套是規模上限，不是能力評價。
- 不變更 `#208` 的 strict closure、foreign exact-head review 或 CompletionRecord 誠信要求。
- 不實作 `#137` 的 `track_record()`；R1 項 6 只引用其簽章。

## 驗收面

- 本文件 R1–R8 逐條可對照到 issue #209 原文 §8.1／§8.2／§5／§9.5 的對應段落，且每條更正
  （R3／R6／R8）在拿掉後可還原成 issue 原文或其修正 comment 的表述——用以確認落差確實被
  記錄而非漏抄。
- `grep -rn "accepts_bands\|invariant_ceiling\|consistency_scope\|acceptance_modes"` 現況應
  只命中本次新增的設計文件，不命中任何 `.py` 檔（本票不實作）。
- `model_identities.py:115-122` 現有欄位白名單（`executor`／`model_id`／
  `independence_domain`／`capabilities`／`live_probe`）與本票新增四欄位名稱無命名衝突（逐一
  比對確認）。
- 後續實作票（R3 落地票／`capable()` 實作票／judge 整合票）的驗收條件由各自 issue 訂定；本票
  不承諾任何可執行的驗收命令。
