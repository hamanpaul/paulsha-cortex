---
status: accepted
work_item: design-model-capability-envelope
---

# design-model-capability-envelope Design

## Decisions

### D1 `capable()` 為六項合取式，逐項標註來源與是否已有落地片段

`capable(resource, work)` 定案為六項判準的 AND，不做加權評分（比照 `#138` MVP「滿足條件即選
一個資源」的既定形態，不做優化器）：

1. `work.sizing_band ∈ resource.accepts_bands` —— 來源 `#208`，`sizing_band` 已由
   `claim.py:1098` 的 `sizing_band()` 產出，資料源已存在。
2. `work.invariant_count ≤ resource.invariant_ceiling` —— `work.invariant_count` 這個值
   本身已被 `planning.py:462-464` 的 `_plan_review_envelope` 消費（plan frontmatter 宣告），
   但目前是拿它跟 plan 自己宣告的「envelope」比（一個 `envelope_lookup()` provider 回傳的
   Mapping），不是跟 resource registry 裡的 `invariant_ceiling` 比。本票新定義的是「跟哪個
   resource 比」這一段連結，不是這個欄位本身。
3. `work.artifact_classes ⊆ resource.consistency_scope` —— 同上，`artifact_classes` 集合
   語意已在 `planning.py:465-472` 落地，本票新定義的是拿它跟 resource 的
   `consistency_scope` 比較。
4. `work.acceptance_mode ∈ resource.acceptance_modes` —— 全新，repo 內無對應既有欄位（易與
   `acceptance_surfaces` 混淆，見 D8）。
5. `work.required_capabilities ⊆ resource.capabilities` —— 已落地（`#130`），
   `ModelIdentity.capabilities`（`model_identities.py`）。
6. `track_record(resource, work.task_type) ≥ threshold` —— 來源 `#137`，尚未落地
   （`grep -rn "track_record" paulsha_cortex` 零命中）；本票只固定其簽章型別
   `Callable[[Resource, str], float]`，不假設實作細節、不假設 `threshold` 的數值或來源。

理由：六項裡有兩項（2、3）其實已經有「work 側資料生產」的落地程式碼，只是還沒有「resource
側比較對象」；把這個事實寫清楚，能讓後續實作票精確知道自己只差「resource 側查表」這一半，而
不是從零開始設計欄位語意。

風險與緩解：若後續實作票誤以為六項全新，可能重複發明 `invariant_count`／`artifact_classes`
的產出邏輯——緩解：R7（既有消費端契約點）與 R1 的來源欄已明確標註「已消費」與「已落地」，
code review checklist 可據此核對是否 import 既有函式而非重寫。

### D2 四個靜態欄位掛在 `(executor, model_id)` 複合鍵上

`accepts_bands`／`invariant_ceiling`／`consistency_scope`／`acceptance_modes` 四欄位定案
掛在 `(executor, model_id)` 複合鍵，型別／值域見 spec R2。

理由：`model_identities.py` 的 `IdentityRegistry.get(executor, model_id)`（第 159 行起）與
`from_rows` 的重複鍵判定（`key = (executor, model_id)`，約第 149-152 行）已經是這條複合鍵的
唯一真相源；`executor` 是 launcher（`agy`）不是 vendor（見 `model_identities.py` 頂部關於
`AGY_MODEL_ID` 的註解），同一 `executor` 下可有多個 `model_id`，掛在單獨 `executor` 上會讓
不同 model 的封套混在一起。四欄位若掛在單獨 `model_id` 上則會忽略「同一 model_id 換一個
launcher 執行，實測封套可能不同」的可能性（雖然目前 roster 沒有這種案例，但複合鍵設計不需要
等到出現才修）。

### D3 短期不新建 `resource-inventory.yaml`，併入 `model-identities.yaml`

四個新欄位 SHALL 擴充既有 `model-identities.yaml`／`model_identities.py`
（schema version 由 2 升 3），MUST NOT 新建一個 issue §8.2 提到但實際 owner／schema 皆未定案
的 `resource-inventory.yaml`。

理由：本票逐字核對 `#139`（issue body）的落地任務清單——六項（taxonomy／log reader／
`project_resolver` 歸屬／resource status view／outcome ledger／session-health）沒有一項是
「新增一份靜態 inventory 檔案」。`resource-inventory.yaml` 這個檔名只出現在 `#209` 自己的
issue 正文與 `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 對 `#209` 的
轉述裡，從未在 `#139` 的任務清單裡出現過對應項目。與此同時 `model-identities.yaml` 已經是
repo 內唯一有 loader、有 fail-closed 驗證、複合鍵語意完全吻合的位置——多繞一層去等一個尚未
定案的檔案，只會讓 `#209` 的落地卡在 `#139` 的排程上，而兩者事實上互不相依（`#209` 需要的是
「哪裡有一個 per-identity 的 config 表」，`#139` 要解決的是「跨家 log 的動態 JOIN」，兩者資料
生命週期完全不同：一個是靜態 config、一個是動態衍生視圖）。

風險與緩解：若 `#139` 未來真的要求把所有 resource 相關 config 集中到一個新檔案，本票的落地
會需要一次遷移——緩解：R3 已明文要求遷移必須是 additive-only（欄位名／值域語意不得變動），
只是換檔案位置，不是改契約，遷移成本可控。

### D4 三閘序對齊既有裁決，並記錄 `CHECK_ORDER` 尚未真正分岔的落差

eligibility／admission／routing 三閘序（spec R4 表格）直接沿用 issue §9.5 與
`cost-governance-cluster/todo.md`「已定案」第 5 條的既有裁決，本票不重新開放討論；本票的
增量貢獻是把這個裁決對照到 `claim_readiness.py:57-64` 的既有 `CHECK_ORDER`，找出一個具體
落差：

`CHECK_ORDER` 目前是一條線性交易，六個檢查（`local_scope`／`base_sha`／`monitor_snapshot`／
`github_owner`／`capability`／`live_probe`）共用同一套 `ReadinessOutcome`
terminal/retryable 二分類（`claim_readiness.py` 模組 docstring 明載：只有
policy-scope-conflict 是 terminal，其餘皆 retryable）。但 R4 的三閘模型要求 admission
失敗要走**第三種**結局——「不擋，排隊＋控速」，不是 terminal 也不是簡單 retryable（retryable
目前的下游語意是 Manager 既有 `blocked` 詞彙，語意上更接近「這次不行，之後可以重試整個
transaction」而非「進佇列排隊」）。`capability` 檢查（現在 bypass 恆真）一旦真的接上
`capable()`，其失敗語意屬於 eligibility（該擋），這與現行分類相容；但 `live_probe`
（第六格，真正啟動一個 model session）性質上同時橫跨 admission（quota／rate 是否夠）與
routing（選中的就是要 probe 的那個），現行分類並未區分。

理由把這個落差寫進設計文件而非留白：讓後續把 `capable()` 接上 `claim_readiness.capability_probe`
的實作票，明確知道「單純把 `capability_lookup` 填上」還不足以完整落地 R4 的三閘語意——是否
需要在 `CHECK_ORDER` 之外新增一層 admission 排隊機制，是那張票要處理的範圍，不是「填一個
lookup 函式」就能蓋括的。

風險與緩解：若實作票誤以為只要接上 `capability_lookup` 就完成三閘落地，會漏掉 admission
「不擋只排隊」的語意——緩解：本節與 spec R4 明文列出這個落差，未來 code review 可對照本文件
逐項核對。

### D5 topic×band 矩陣現況只有 eligibility 語意

issue §5 矩陣原樣保留（本票不重新推導 topic 分佈或門檻），但本票新增一條明文限制：矩陣在
「registry 只有一個具 `build` capability 身分」的現況下，只能回答「該不該派」（eligibility），
不能回答「派給誰」（routing）——因為分母只有 1。`red` 帶「不可單一 build 身分 end-to-end」
的結論本身是 eligibility 判斷（意思是「不可直接派給任何單一身分，需要先拆分」），不受這個
限制影響，因此矩陣現在仍有實用價值，只是價值集中在 eligibility 這一格。

理由：避免後續實作票誤以為矩陣落地後就能自動產生「在多個 builder 間選一個」的效果——那要等
roster 擴充（新增至少一個具 `build` capability 的第二身分）才會發生，且擴充時程不在本票掌握
範圍內（見「現況更正」）。

### D6 registry 現況更正：packaged registry 只有 1 身分，但與同一 repo 內一份受版控筆記矛盾

`model-identities.yaml` 全文只有一個身分（`agy`/`gemini-3.1-pro-high`/`capabilities:
[planning]`），這點本票以檔案原文為準（見 spec 背景節的完整 YAML 引用）。issue #209 的
2026-07-27 comment（`§4 更正`）宣稱 registry「實際三個身分」，列出
`agy`/`claude-sonnet-4-6`（build）、`agy`/`gemini-3.6-flash-high`（review）、
`agy`/`Gemini 3.1 Pro (High)`（planning）。

本文件初版曾誤述「本票對 main 全 repo（含 tests fixtures）執行 grep，零命中」——複驗更正：
`grep -rn "claude-sonnet-4-6\|gemini-3.6-flash-high" .`（排除 `.git`）在 main 上**確有
命中**，且不只 issue comment 文字：

- `docs/superpowers/workstreams/cost-governance-cluster/todo.md:129` 是本 repo 既有、
  受版控的「關鍵事實（避免重犯）」筆記，白紙黑字寫「registry 實際只有三個身分…
  `claude-sonnet-4-6`（build/anthropic）、`gemini-3.6-flash-high`（review/google）、
  `Gemini 3.1 Pro (High)`（planning/google）」，與 issue comment 的修正表幾乎逐字一致。
- `docs/superpowers/workstreams/driving-cortex-skill/todo.md:12` 另有 1 處提及
  `gemini-3.6-flash-high` 作為 ForeignReview 執行身分。
- `tests/test_model_identities.py:368,376` 是測試 fixture 裡字面湊巧出現的字串（mock CLI
  輸出／診斷訊息斷言用途），與 registry 身分宣告無關，不構成同語意命中。

這代表本文件 R4／spec R4 引用的同一份 `cost-governance-cluster/todo.md`（三閘序「已定案」
第 5 條）與本節（registry 只有 1 身分）之間存在一個未收斂的矛盾：**同一份 todo.md** 在第 5
條被本票奉為三閘序的權威來源，卻在第 129 行斷言一個與 packaged registry 不符的三身分表。
本票不假裝這個矛盾不存在，也不擅自替 todo.md 的 owner 收斂它，本文件的立場是：

1. `model-identities.yaml`／`model_identities.py` 是 repo 內**唯一有 loader、有
   fail-closed 驗證、被程式實際讀取**的 packaged registry 路徑，本票 R1–R8 的資料依據以它
   的現況（1 身分）為準；
2. `todo.md:129` 的三身分表*可能*反映某個 host-local
   `$PSC_PROJECT_CONFIG_ROOT/model-identities.yaml` overlay（該路徑允許自訂疊加，見
   `model_identities.py` 對 packaged vs custom 兩份檔案的合併邏輯）在記錄當下被筆記者當成
   「registry 現況」寫下——但這是本票的推測，不是查證結論：`todo.md:129` 本身沒有標注資料
   來源是 packaged 檔案還是某台機器的 overlay；
3. 這個矛盾 MUST 在後續實作票（欄位 schema PR）上線前與 `cost-governance-cluster` todo
   owner 對齊：若 overlay 確實已在某環境生效提供三身分，「僅 1 身分」這個風險評估前提就需要
   重新核實；若 `todo.md:129` 是過時或誤記，該筆記應更新以避免持續誤導。本票不擅自編輯
   `cost-governance-cluster/todo.md`（超出本票 owner 權限與範圍）。

理由記錄這一層而非止步於「查了 grep」：往後任何要引用「registry 現況」的實作票，若只讀
issue comment 或只讀 todo.md 其中一份文件，可能各自得到不同答案——本文件把兩份文件的矛盾
攤開，並明確標注查證結論的信心邊界（packaged 檔案內容可重跑核對；host-local overlay 只是
假設），避免任一方被誤當成唯一權威、也避免「零命中」這種可證偽陳述誤導後續讀者。

風險與緩解：若之後證實 `todo.md:129` 反映的是某個已在用的環境現況（而非過時筆記），本文件
「僅 1 身分」的假設會低估現行實際可用的 build/review 身分數，`capable()` 上線影響評估
（見「風險與緩解」章節）需要跟著重估——緩解：本節已明文列出這是待收斂項，且要求後續實作票
上線前重新確認 registry 現況，不是本文件片面決定終局。

### D7 沿用既有 `envelope_lookup` 介面形狀，不另起爐灶

`planning.py:456-509`（`_plan_review_envelope`）＋
`plan_review_gate(..., envelope_lookup=...)` 已是 `#212` 落地的既定 provider 介面：
`Callable[[], Mapping[str, object] | None]`，回傳 Mapping 至少含 `invariant_count: int`／
`artifact_classes: list[str]` 兩鍵。`manager.py:5988` 目前固定 `envelope_lookup=None`（永遠
bypass，見 `_evaluate_yellow_plan_review` docstring 的 fail-soft 說明）。

理由把這條寫進設計文件：`capable()` 供給端實作票如果不知道這個既有掛勾，很可能會在
`plan_review_gate` 之外另開一條「查 resource envelope」的路徑，造成 plan-review 階段
（Yellow band 推進 build 前）與 claim-readiness 階段（`claim_readiness.capability_probe`）
分別用兩套不同格式查同一份 registry，形成漂移風險。正確做法是同一個 provider 函式（讀
`model-identities.yaml` 新欄位）投影出兩種呼叫端期望的形狀：`claim_readiness` 要的是
`bool | None`（`capability_lookup: Callable[[str], bool | None]`，見
`claim_readiness.py:421-422`），`planning` 要的是
`Mapping[str, object] | None`（含 `invariant_count`／`artifact_classes`）。兩者都是同一份
resource 封套資料的不同切面，實作票 SHALL 共用底層查表，只在介面層做形狀轉換。

風險與緩解：兩個呼叫端各自要求不同回傳型別，容易被實作成兩份獨立邏輯而漂移——緩解：本節與
spec R7 明文要求「共用底層查表」，並指出兩處既有函式簽章（含檔案:行號），供 code review
逐一核對呼叫鏈末端是否真的收斂到同一份資料源。

### D8 `acceptance_mode` 與既有 `acceptance_surfaces` 的命名消歧

repo 內已有 `acceptance_surfaces`，且有兩種既有語意並存：`SizingScore.acceptance_surfaces:
int`（`planning.py:618,668-696`，H.1 五維分數其中一維，門檻分三級）與
`plan_review_gate(acceptance_surfaces: frozenset[str], ...)`（`planning.py:518`，拿 Tasks
逐項核對用的集合）。issue §8.1／§8.2 的 `acceptance_mode`（work 單值枚舉）／
`acceptance_modes`（resource 集合）在拼字上與這兩個既有用法都相似但語意都不同（「驗收手段」
而非「產物覆蓋數」或「規則覆蓋集合」）。

本票裁定：保留 issue 原文的欄位命名（`acceptance_mode`／`acceptance_modes`），不因為與既有
`acceptance_surfaces` 相似而改名——因為改名會導致本文件與 issue 原文的可追溯性斷裂，且四個
候選值（`focused_tests`／`repo_gate`／`live_evidence`／`github_closure`）與既有
`acceptance_surfaces` 的三級門檻語意本來就不是同一個概念，不存在「合併成一個欄位」的空間。
取而代之的緩解手段是文件層面的顯式消歧（本節＋spec R8），並要求後續實作票的 docstring／
變數命名不得縮寫成容易與 `acceptance_surfaces` 混淆的形式。

風險與緩解：兩個相似命名長期並存本身就是漂移風險——緩解：本節是唯一一處同時解釋兩者差異的
文件，後續任一方修改時的 code review checklist 可要求連帶檢查本節是否仍準確。

### D9 `track_record()` 只固定簽章，不假設 `#137` 的實作

`threshold` 的數值來源、`track_record()` 內部如何計分（棘輪／滑動視窗／其他）完全留給
`#137`，本票不預先決定。理由：`#137` 是本批（W4 design 票）內平行進行的另一張設計票，
`#209` 與它之間只需要一個穩定的函式簽章介面，不需要提前綁定實作細節；提前假設只會在 `#137`
的設計定案後產生不必要的修約成本。

## 風險與緩解

- **`model-identities.yaml` 升版（schema v2→v3）與現行唯一身分（僅 `planning`
  capability）疊加**：`capable()` 落地當下若沒有先補一個 `build` capability 身分帶齊四個
  新欄位，會讓現行 build 派工從「無過濾」變成「全部擋下」——緩解：R6／D6 已明文要求後續實作票
  上線前必須先確認至少一個 `build` 身分已補齊四欄位，不得盲目上線判準。
- **`resource-inventory.yaml` 命名如果被誤解為「已存在待改」**：任何人快速掃 issue 原文可能
  誤以為只要去改一個現成檔案——緩解：D3／R3 明文查證該檔案不存在，並給出替代落地位置與遷移
  路徑。
- **`CHECK_ORDER` 三閘語意落差被實作票忽略**：只接上 `capability_lookup` 而不處理 admission
  排隊語意，會讓 `#136`/`#138`/`#209` 三閘裁決名存實亡——緩解：D4／R4 明文列出落差位置
  （`claim_readiness.py:57-64`），供落地票對照。
- **`acceptance_mode` 與 `acceptance_surfaces` 混淆**：兩者拼字相似、都與「驗收」相關——
  緩解：D8／R8 集中說明，並要求變數命名不縮寫。
- **track_record() 介面提前綁死**：若 `#137` 定案的簽章與本票假設不同，`capable()` 第六項
  需要修約——緩解：D9 明文只依賴簽章型別，`#137` 定案後若簽章不同，只需改本票 R1 表格第 6
  列，不影響其餘五項。
