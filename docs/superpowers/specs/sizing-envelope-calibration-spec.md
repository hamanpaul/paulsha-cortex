---
status: proposed
work_item: sizing-envelope-calibration
---

# sizing-envelope-calibration Specification

#210：把 `#209` 供給側封套四欄位（`accepts_bands`／`invariant_ceiling`／`consistency_scope`／
`acceptance_modes`）從**手估**改為可用 **cortex 自身 run 歷史**校準，並把 `#208` sizing 宣告
的先驗（planner 宣告）補上後驗（歷史 diff LOC 中位數）。方法參考
`hamanpaul/paulsha-patchmud` 的量表設計，**僅為方法參考，不建立任何跨 repo 依賴、不消費其
產物**。**本票是設計文件，不實作任一 estimator、不改 `model_identities.py`／
`completion.py`／`claim.py` 任何一行程式碼、不新增資料源。**

## 背景

`#209`（本票唯一前置依賴，`depends_on: [209]`）已定案供給側四欄位的型別／值域／
`(executor, model_id)` 複合鍵契約與落地位置（`model-identities.yaml` schema v2→v3），但
`#209` 本身也只是設計文件——四欄位尚未實際寫入任何 `.py` 或 `.yaml`（`grep -rn
"accepts_bands\|invariant_ceiling\|consistency_scope\|acceptance_modes" paulsha_cortex/`
在 main @ `a2e8d0c` 仍是零命中，只命中 `docs/superpowers/specs/design-model-capability-
envelope-{spec,design}.md` 這兩份設計文件本身）。**因此本票同樣沒有供給側欄位可掛
`calibration_source`／`calibrated_at`——這是 `#209` 遺留給任何後續票的共同起點，不是本票
獨有的落差。**

需求側已有的、可供後驗使用的原始資料（main 現況核對）：

- `paulsha_cortex/coordinator/claim.py:1098` `sizing_band()`：五維總分 → band，`#208`
  落地並生產可用。
- `paulsha_cortex/coordinator/delivery.py:46-68`（`REPAIR_BUDGET_BY_BAND`／
  `repair_budget_for_band()`）：issue §2.4／§4「repair 上限依 band 分級」**已 100% 落地**
  （`#218`），green=1／yellow=2，red 防禦性拒絕（`delivery.py:63-67`）。本票**不重做**這項。
- `paulsha_cortex/coordinator/planning.py:462-472`（`_plan_review_envelope`）：plan
  frontmatter 已強制宣告 `invariant_count`（`int ≥0`）與 `artifact_classes`（非空字串
  list），並與 `envelope_lookup()` provider 回傳的 Mapping 比對（`over_budget` 計算）。issue
  §2.2「`invariant_count` 逐條可數」的雛形**已落地**，但比較對象是 plan 自己宣告的封套
  （`manager.py:5988` 目前固定 `envelope_lookup=None`，恆走 bypass），不是跟
  resource registry 的 `invariant_ceiling` 比——這正是 `#209` R7／D7 已記錄的既有掛勾，
  本票不重複記錄。
- `paulsha_cortex/coordinator/completion.py:49-57,274-283,382-385`
  （`SIZING_DECLARATION_DRIFT_FIELDS`／`_normalize_sizing_declaration_drift`）：
  `CompletionRecord` 已有可選欄位 `sizing_declaration_drift: {declared_modules, actual_modules}`，
  註解明寫「供 `#210` 後驗」。**但 `grep -rn "sizing_declaration_drift" paulsha_cortex/`
  只有寫入端（`completion.py` 本身），無任何讀取／彙總端**——即這份資料目前寫入後沒有任何
  estimator 消費它。且它的粒度是「宣告模組數 vs 實際變更模組數」，**不是** issue §2.1
  patchmud 方法引用的「diff LOC 中位數」，見 R1 的粒度落差記錄。

現況比對表（issue 原文 §4 交付項目 vs main 現況）：

| issue §4 交付項 | main 現況 |
|---|---|
| `resource-inventory.yaml` 新增 `calibration_source`／`calibrated_at` | 檔案不存在（`#209` R3 已定案改用 `model-identities.yaml`，四欄位本身也未落地） |
| 難度後驗 estimator（diff LOC 中位數） | 無任何程式碼；`sizing_declaration_drift` 粒度是模組數非 LOC，且無讀取端 |
| `invariant_ceiling` estimator（通過率曲線） | 無任何程式碼；且 `invariant_count` 本身**未被 `CompletionRecord` 持久化**（僅存在於 plan-review 當下的一次性比對，見 R3） |
| `invariant_count` 逐條編號驗收機制 | 已落地（`planning.py:462-472`，`#212`），本票不重做 |
| `consistency_scope` glob 契約 | 未落地；依附 `#209` 供給側欄位（`consistency_scope` 是集合非 glob，見 R2 對 issue 原文用詞的更正） |
| repair 上限 band 化 | 已落地（`delivery.py:46-68`，`#218`），本票不重做 |
| `cortex stat` 顯示校準來源 | 未落地；`cortex stat` 現有 `--retry-classifications`／`--decomposition-depths`／`--combo-selections`／`--usage-by-run`（`cli.py:109-137`）四個既有彙總旗標可供沿用同一模式，見 R5 |

## Goals

- 定案 `calibration_source`／`calibrated_at` 的掛載範圍：對照 `#209` 四欄位逐一檢視「是否
  存在可回推的歷史資料」，收斂只掛在確有校準方法的欄位上，不是無差別掛在全部四欄位（R1）。
- 定案難度後驗 estimator 的資料來源、對齊 issue 原文方法與 cortex 實際持久化資料之間的
  粒度落差，並給出彌合路徑（R1）。
- 定案 `invariant_ceiling` 估計所需的歷史資料目前**不存在**的持久化缺口，並定案彌補方式
  （R3）。
- 定案 `consistency_scope` 的 glob 化範圍與 `#209` 既有集合定義的相容性（R2）。
- 定案 estimator 的觸發時機與計算模式：比照既有 `cortex stat` 彙總旗標的即時查詢模式，而非
  背景批次腳本（R4／R5）。
- 定案樣本不足時的 fail-soft 規則，逐字對齊 issue §2.5／§3 校準紀律（R6）。
- 明確切分本票（設計）與後續實作票的邊界，並給出可派工的票序（見 `tasks.md`）。

## Requirements

### R1 `calibration_source`／`calibrated_at` 只掛在 `invariant_ceiling`，不掛在其餘三個 `#209` 欄位

`calibration_source: "estimated" | "measured"` 與 `calibrated_at: <ISO8601 timestamp | null>`
两欄位 SHALL 只掛在 `invariant_ceiling` 上（掛載鍵沿用 `#209` R2 的 `(executor, model_id)`
複合鍵），MUST NOT 無差別掛在 `accepts_bands`／`consistency_scope`／`acceptance_modes` 上。

理由：issue §2–§3 給出的**唯一**具體校準方法（通過率 vs `invariant_count` 曲線衰減點，
§3）只回答「`invariant_ceiling` 這個數字該是多少」，`accepts_bands`（該身分接受哪些 band）
與 `acceptance_modes`（該身分支援哪些驗收手段）在本票範圍內**沒有對應的歷史資料能回推**——
它們描述的是身分的**能力邊界宣告**（operator 決定「這個身分可以做到什麼」），不是像
`invariant_ceiling` 一樣「這個身分實際做到過幾條同時不變量」可從 run 歷史統計得出的量。
`consistency_scope` 同樣是宣告集合而非可從 diff 內容直接反推的統計量（見 R2，本票僅收斂
glob 契約的欄位語意，不定義如何從歷史 diff 反推 scope 集合）。issue §5「`resource-inventory.
yaml` 每個封套數值可追溯至『手估』或『run 集合 + hash』」的原文讀作「每個**受本票估計方法覆蓋
的**封套數值」，而非要求對所有四欄位都發明一套校準方法——若照字面對其餘三欄位也發明校準來源，
會產生「宣告一件事的 domain 邊界時附上『估計』標籤」這種語意錯位（`accepts_bands` 本質是
policy 決定，不是待驗證的觀測值）。

風險與緩解：若後續實作票誤以為四欄位都要有校準來源，會做出無意義的
`calibration_source: estimated` 標籤（例如給 `accepts_bands` 標「未校準」，但它從來就不是
一個要被校準的量）——緩解：本節與 tasks.md 明確列出只有一個掛載欄位，供 code review 核對。

### R2 難度後驗 estimator：資料來源改為 `merge_commit` 本地 diff，不沿用 `sizing_declaration_drift`

難度後驗 estimator（issue §2.1，patchmud 方法：`clamp(median(成功 run 的 final diff LOC) /
40, 0.5, 4.0)`，成功數 <3 維持 1.0 並標註未校準）SHALL 以
`CompletionRecord.work_authority.merge_commit`（`completion.py:229` 附近，`_normalize_git_sha`
驗證過的 40 字元 SHA，位於 cortex **自身** repo 的 default branch 歷史上）為 diff 來源，
以 `git diff --shortstat <merge_commit>^..<merge_commit>` 對本地 clone 取得該次交付的
增刪行數，MUST NOT 依賴 `sizing_declaration_drift`（`declared_modules`／`actual_modules`）
作為 diff LOC 的資料源。

理由：`sizing_declaration_drift` 的粒度是「宣告模組數 vs 實際變更模組數」（整數個數），與
issue §2.1 patchmud 方法要求的「diff LOC 中位數」不是同一個量綱——用模組數個數除以 40 沒有
統計意義。`merge_commit` 已是 `work_authority` 必要欄位（`completion.py` `_normalize_work_authority`
required 集合含 `merge_commit`），且 cortex 對自己 repo 有本地 git 歷史可直接 `git diff`，
不需要呼叫 GitHub API（無外部前置，符合 issue #210 comment 「零外部前置，可立即開始」的
更正範圍）。`sizing_declaration_drift` 保留其原有用途（度量宣告失準本身，即
`#208`/`#222` 定義的另一種訊號），與難度後驗 estimator 是兩件事，不得混用。

### R3 `invariant_ceiling` estimator 依賴的歷史資料目前不存在，需先補一個持久化欄位

`invariant_ceiling` 估計方法（issue §3：橫軸 `invariant_count`、縱軸一次通過率，通過率明顯
衰減的 `invariant_count` 即該身分的 `invariant_ceiling`）SHALL 讀取每次已交付 run 的
`invariant_count`（plan 階段宣告值）與其一次通過與否（`retry_classification` 是否含
`model_repair`，見 R4）。**但 `invariant_count` 目前只存在於 `planning.py:462-472` 的
plan-review 當下一次性比對，未被 `CompletionRecord` 或任何其他持久化結構保留**——
`grep -rn "invariant_count" paulsha_cortex/` 只命中 `planning.py`，`completion.py` 的
`SIZING_DECLARATION_DRIFT_FIELDS`／既有 required／optional 欄位集合都沒有這個鍵。

本票 SHALL 定案：`invariant_ceiling` estimator 的前置依賴是**新增一個 `CompletionRecord`
可選欄位**（暫定名 `plan_invariant_count: int`，比照 `sizing_declaration_drift` 的「可選
欄位＋`_normalize_*`＋extras 白名單聯集」既有慣例——具體欄位名與型別驗證細節留給後續實作票
定案，不在本票凍結),記錄該次 completion 對應的 plan 宣告 `invariant_count` 快照。這是本票
發現的一個**新缺口**，issue 原文未提及，`#222`（落地 `sizing_declaration_drift`）當時的
範圍也不含它——`#222` 只處理模組數宣告-實際落差，不含不變量數的歷史留存。

理由：沒有這個欄位，`invariant_ceiling` estimator 完全無資料可用，即便有其餘資料源（成功
與否、身分別）也無法畫出「通過率 vs `invariant_count`」曲線——這是本票必須誠實記錄的一個
硬性阻塞點，不能假裝 R4 的資料源已經足夠。

風險與緩解：若後續實作票沒看到本節就直接開工，會在 estimator 實作階段才發現資料源缺失，
返工成本較高——緩解：本節與 tasks.md「建議後續實作票切分」把「補
`plan_invariant_count` 持久化欄位」列為第一張前置票，明確標注其為 estimator 票的硬性依賴。

### R4 「一次通過率」定義：以 `retry_classification` 是否含 `model_repair` 為準

R3 的「一次通過率」SHALL 定義為：分母＝該 `(executor, model_id)` 身分下全部
`CompletionRecord`（即已交付、有 `builder_job_id` 可回溯 job 記錄的 completion）；分子＝
其中 `retry_classification` 欄位缺席，或存在但值不等於 `"model_repair"` 的筆數。

理由：`retry_classification`（`completion.py:36-44`，`RETRY_CLASSIFICATION_VALUES`）的五值
中，只有 `model_repair` 描述「builder 模型自己修正過」這件事；其餘四值
（`orchestrator_retry`／`authority_restart`／`review_handoff_failure`／
`source_owner_repair`）描述的是編排層或環境層的重跑原因，與「builder 這次交付是否需要
model 自我修正」無關，計入分母的失敗會誤傷 `invariant_ceiling` 的估計（把編排層抖動誤記
為模型能力不足）。`(executor, model_id)` 身分別 SHALL 透過 `builder_job_id` JOIN
registry.py 的 job 記錄取得（`registry.py:929-931` `job["executor"]`／`job["model_id"]`），
沿用 `cli.py:68` 既有的「依 `workflow_run_id` join 補欄」慣例（同一模組已有先例，不另起
爐灶）。

### R5 estimator 觸發時機：比照既有 `cortex stat` 彙總旗標，即時查詢而非背景批次

難度後驗 estimator 與 `invariant_ceiling` estimator SHALL 以與 `cortex stat` 現有四個彙總
旗標（`--retry-classifications`／`--decomposition-depths`／`--combo-selections`／
`--usage-by-run`，`cli.py:109-137`）相同的模式落地：**每次呼叫時即時掃描
`evidence/completion/*.json` 與 `registry.py` job 記錄計算**，不落地為背景批次腳本、不產生
另一份快取／彙總檔。

理由：既有四個旗標全部是「查詢當下重新計算」模式（`aggregate_usage_by_run` 等函式簽章皆吃
`list[dict]` 現況資料，不讀取任何預先算好的彙總檔），`CompletionRecord` 本身是 append-only
durable evidence（`completion_record_path`，`completion.py:72-83`），資料量隨 cortex 自身
派工量增長但屬本機檔案系統掃描，即時計算的成本可接受，且避免了「快取何時失效」這個額外
狀態管理問題。若未來資料量成長到即時計算不可接受，才由後續票另行決定是否加背景批次——本票
不預先假設需要。

### R6 校準紀律：樣本不足即標註 `estimated` 並保留既有值，不得輸出 0 或假造中位數

兩個 estimator MUST 遵守 issue §2.5／§3 逐字採用的紀律：

- 難度後驗 estimator：成功樣本數 `<3` 時，難度尺度維持 `1.0` 並標註未校準（不覆寫既有
  手估值，若尚無手估值則保持缺省）。
- `invariant_ceiling` estimator：任一 `(executor, model_id)` 的完成樣本數 `<3` 時，保留
  `#209` 手估的 `invariant_ceiling` 值，`calibration_source` 維持 `"estimated"`，
  `calibrated_at` 維持 `null`（或既有值不變）。

两 estimator MUST NOT 在樣本不足時輸出 `0`、假造中位數，或以任何方式產生「看似已校準」的
假象。此紀律 SHALL 由後續實作票以測試斷言覆蓋（樣本數邊界值 0／1／2／3 的行為）。

### R7 `consistency_scope` 維持集合語意，不在本票升級為 glob 契約

issue §2.3 建議 `consistency_scope` 寫成 glob（`allowed_paths`／`expected_paths` 式）並與
builder persona `write_paths` 比對。`#209` R2 已把 `consistency_scope` 定案為
**枚舉值的有限集合**（`code`／`test`／`spec`／`openspec`／`changelog`／`docs`／`pr`／
`issue` 八值子集，非 glob 路徑模式），本票 MUST NOT 片面推翻 `#209` 已凍結的型別契約。

理由：`#209` 的 `consistency_scope` 描述的是「產物**種類**」（藝術品 class，如 `code` vs
`test`），glob（`paulsha_cortex/**`）描述的是「檔案**路徑範圍**」，兩者是不同維度，issue
原文其實混用了這兩個概念（§2.3 標題「一致性半徑寫成 glob 契約」但 §8.2 表格定義
`consistency_scope` 的值域是產物種類列舉，不是 glob）。本票裁定：`#209` 的凍結契約
（產物種類集合）優先，issue §2.3 對 builder persona `write_paths` 的 glob 比對如果仍有
必要，SHALL 是一個**獨立**於 `consistency_scope` 之外的欄位／檢查（例如既有 `#118`
`write_paths` 契約本身的強化），不與 `consistency_scope` 混為一談；本票不定案該獨立欄位
是否需要，留給後續 issue 視需要另開。

### R8 `cortex stat` 顯示介面：新增 `--calibration` 彙總旗標

`cortex stat` SHALL 依 R5 的即時查詢模式新增一個彙總旗標（暫定 `--calibration`，具體旗標名
留給後續實作票依 `cli.py` 既有四個旗標的命名慣例定案），輸出各 `(executor, model_id)` 身分
的 `invariant_ceiling`（含 `calibration_source`／`calibrated_at`／樣本數）與難度尺度（含
未校準標註），格式比照既有 `--usage-by-run` 的 `{"usage_by_run": {...}}` 包裹慣例
（`cli.py:537`）。

### R9 交付順序：三張前置票依序解鎖，`#210` 不得插隊改動 `#209` 的落地範圍

`#210` 的 estimator 落地 SHALL 依下列順序，每張票只解鎖下一張，不得跳過：

1. `#209` 的「欄位 schema PR」（`#209` `tasks.md` 已訂：`model-identities.yaml` schema
   v2→v3，新增 `accepts_bands`／`invariant_ceiling`／`consistency_scope`／
   `acceptance_modes` 四欄位，並補至少一個 `build` capability 身分）先落地。
2. 本票 R1 定案的 `calibration_source`／`calibrated_at` 兩欄位（掛在 `invariant_ceiling`
   上，schema v3→v4 或併入同一次升版，由該實作票自行決定版本號策略）與 R3 定案的
   `CompletionRecord` 新欄位（`plan_invariant_count` 或等效名稱）分別落地——這兩者互不
   相依，可平行進行。
3. 難度後驗 estimator（依 R2／R6）與 `invariant_ceiling` estimator（依 R3／R4／R6）分別
   落地，兩者都需要步驟 2 的資料源就緒；`cortex stat --calibration`（R8）在两 estimator
   至少一個可用後即可先行串接（不須等两者都完成）。

`#210` 本票的設計範圍 MUST NOT 因為想加速交付而回頭修改 `#209` 已凍結的欄位契約（型別／
值域／複合鍵）——若執行中發現 `#209` 契約有問題，SHALL 另開票修正 `#209` 本身，不在
`#210` 的實作票裡夾帶變更。

## 非目標

- 不實作任一 estimator、不新增 `plan_invariant_count`（或任何等效）欄位到
  `completion.py`、不改 `model_identities.py`／`claim_readiness.py`／`cli.py` 任何一行
  程式碼。
- 不重做已落地項目：`invariant_count`/`artifact_classes` 宣告與 plan 自我一致性比對
  （`#212`）、repair 上限 band 化（`#218`）、`sizing_declaration_drift` 模組數落差記錄
  （`#222`）。
- 不決定 `#209` 四欄位本身的 schema PR 何時落地（`#210` 的 estimator 需要它先存在，見 R9
  的交付順序，但排程不在本票掌握）。
- 不推翻 `#209` R2 對 `consistency_scope` 的凍結契約（見 R7）。
- 不引用或依賴 `paulsha-patchmud` 任何產物（issue 已於 2026-07-27 comment 更正為
  cortex-only，本票沿用此更正）。

## 驗收面

- 本文件 R1–R8 逐條可對照回 issue #210 原文 §2／§3／§4／§5 對應段落，且每條偏離（R1 收斂
  掛載範圍、R2 改資料源、R3 記錄新缺口、R5 定調查詢模式、R7 維持集合語意）在拿掉後可還原成
  issue 原文的表述，用以確認落差確實被記錄而非漏抄。
- `grep -rn "calibration_source\|calibrated_at\|plan_invariant_count"` 現況應只命中本次
  新增的設計文件，不命中任何 `.py`／`.yaml`（本票不實作）。
- 後續實作票（R3 前置票／難度後驗 estimator 票／`invariant_ceiling` estimator 票／
  `cortex stat --calibration` 票）的驗收條件由各自 issue 訂定；本票不承諾任何可執行的驗收
  命令。
