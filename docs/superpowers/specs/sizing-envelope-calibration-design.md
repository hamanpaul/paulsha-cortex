---
status: proposed
work_item: sizing-envelope-calibration
---

# sizing-envelope-calibration Design

## Decisions

### D1 `calibration_source`／`calibrated_at` 掛載位置：只掛 `invariant_ceiling`，且掛在 `#209` 尚未落地的欄位之上

`resource-inventory.yaml` 這個檔名（issue §4 第一項交付）沿用 `#209` D3／R3 的既有裁決——
不新建，併入 `model-identities.yaml`。本票在此之上新增裁決：`calibration_source: "estimated"
| "measured"` 與 `calibrated_at: ISO8601 | null` 兩欄位 SHALL 只掛在 `invariant_ceiling`
（`(executor, model_id)` 複合鍵），MUST NOT 對 `accepts_bands`／`consistency_scope`／
`acceptance_modes` 三欄位也各自附一份校準來源標記。

理由：issue §2–§3 給出的具體校準演算法（通過率 vs `invariant_count` 曲線衰減點）只回答
`invariant_ceiling` 這一個數字該是多少；其餘三欄位是 operator 對「這個身分被允許做什麼」的
**宣告**，不是可從歷史觀測值統計出來的量——給宣告值貼「estimated/measured」標籤在語意上是
錯位的（宣告沒有「量測」這回事，只有「決定」）。本票不因為要湊齊 issue §5「每個封套數值可
追溯」字面而發明沒有意義的校準來源。

風險與緩解：若後續實作票誤讀 issue §5 為「四欄位都要」，會產生無意義的
`calibration_source: estimated` 標籤在 policy 宣告欄位上——緩解：spec R1 與本節重複強調
只有一個掛載欄位，且給出「宣告 vs 觀測」的判準供未來新增欄位時比照援引（例如若日後真的要對
`acceptance_modes` 做某種驗證式校準，判準是「有沒有一個能從歷史 run 推導它的具體演算法」）。

### D2 難度後驗 estimator 資料源：`merge_commit` 本地 diff，不是 `sizing_declaration_drift`

難度後驗 estimator（patchmud 方法 `median(diff LOC)/40`）SHALL 讀
`CompletionRecord.work_authority.merge_commit`（`completion.py` `_normalize_work_authority`
required 欄位），在本地 clone 對該 SHA 執行 `git diff --shortstat <sha>^..<sha>` 取增刪行數；
MUST NOT 把 `sizing_declaration_drift.{declared_modules,actual_modules}`（模組**個數**）
當成 diff **行數**的替代資料源。

理由：本票查證發現一個 issue 原文與現況的粒度落差——issue §2.1 沿用 patchmud
「diff LOC 中位數」的公式，但 cortex 目前唯一貼著「供 #210 後驗」註解落地的欄位
（`completion.py:53` 附近的 `sizing_declaration_drift`，`#222`）量的是模組數而非行數。
兩者不能互換：一個 PR 可能只改 1 個模組但動了 500 行，也可能改 5 個模組但每個只加 1 行，
拿模組數除以 40 會得出與 patchmud 原始方法完全不同語意的數字。`merge_commit` 是既有
`work_authority` 必要欄位、且 cortex 對自己 repo 本來就有完整本地歷史，取 diff LOC 不需要
新資料源、不需要呼叫 GitHub API——延續 issue #210 comment 2026-07-27 的「零外部前置」更正
精神。

風險與緩解：若後續實作票沒注意到這個粒度落差，直接拿 `sizing_declaration_drift` 套用公式，
會產出一個數值上「看起來能跑」但統計意義錯誤的難度尺度——緩解：本節與 spec R2 明文記錄兩者
不可互換，並指名正確資料源（`merge_commit` + 本地 `git diff`）。

### D3 `invariant_ceiling` estimator 的硬性阻塞點：`invariant_count` 從未被持久化

本票查證的**新發現**（issue 原文與 main 現況都未提及）：`invariant_ceiling` estimator
（issue §3：橫軸 `invariant_count`，縱軸一次通過率）需要「每次已交付 run 對應的
`invariant_count` 宣告值」這份歷史資料，但 `invariant_count` 目前**只存在於
`planning.py:462-472` 的 plan-review 當下一次性比對**（`_plan_review_envelope` 拿 plan
frontmatter 的值跟 `envelope_lookup()` 比對後即結束，不寫入任何持久化結構）。
`CompletionRecord` 的既有可選欄位集合（`work_authority`／`reused_from`／
`retry_classification`／`final_defect_locus`／`sizing_score`／`sizing_band`／
`sizing_declaration_drift`）沒有一個承載這個值。

本票裁定：`invariant_ceiling` estimator 的**前置依賴**是新增一個 `CompletionRecord` 可選
欄位（暫定 `plan_invariant_count: int`，比照 `sizing_declaration_drift` 的既有慣例——可選
欄位＋`_normalize_*`＋extras 白名單聯集），本票只定案「需要補這個欄位」與「比照哪個既有慣例」，
不凍結最終欄位名與型別驗證細節（留給該前置票在实作時定案，因為屆時可能發現與 `#209` 的
`invariant_ceiling` 本身要不要共用同一個「invariant」詞根有更合適的統一命名）。

理由把這件事寫成獨立決策而非埋在 R3 附註裡：這是本票查證過程中發現的**唯一一個會讓
`invariant_ceiling` estimator 完全無法動工**的缺口——不像難度後驗 estimator（D2）只是資料
源選錯，`invariant_ceiling` estimator 是**沒有資料可用**。如果不明確標註，後續實作票可能
會在寫測試時才發現「歷史上到底哪些 run 有幾條不變量」根本查不到，届時才回頭補欄位，多繞一輪
PR。

風險與緩解：若補欄位的前置票與 estimator 票分屬不同 PR 由不同人接手，可能各自對欄位名／
型別有不同假設——緩解：本節與 spec R3／R9 明文要求兩者依序落地且欄位名由前置票統一定案，
estimator 票 SHALL 直接引用前置票的既定命名，不得自創替代欄位。

### D4 「一次通過率」的操作型定義：排除非 model_repair 的 retry 原因

`invariant_ceiling` estimator 的分子（一次通過的 run 數）SHALL 定義為
`retry_classification` 欄位缺席，或存在但值 `!= "model_repair"` 的 `CompletionRecord`
筆數；`(executor, model_id)` 身分別透過 `builder_job_id` JOIN `registry.py` job 記錄
（`registry.py:929-931`）取得，比照 `cli.py:68` 既有 join 慣例，不另立第二套 join 邏輯。

理由：`RETRY_CLASSIFICATION_VALUES` 五值中只有 `model_repair` 描述「模型自己修正錯誤」；
其餘四值（`orchestrator_retry`／`authority_restart`／`review_handoff_failure`／
`source_owner_repair`）是編排層或環境層原因，與「這個身分能不能一次扛住 N 條同時不變量」
無關。若不排除這四類，estimator 會把「daemon 重啟導致的 retry」也算進「模型沒一次通過」，
系統性低估 `invariant_ceiling`。

風險與緩解：若未來 `#137`／`#215` 對 `retry_classification` 的枚舉值有增補，本節判準
（「是否等於 `model_repair`」）需要跟著檢查是否仍完整涵蓋「模型自身修正」的語意——緩解：
本節明文只依賴單一列舉值比對，枚舉新增值時只需確認新值是否也屬於「模型自身修正」類別，
不需要重新設計整個判準邏輯。

### D5 estimator 觸發時機：即時查詢，比照 `cortex stat` 既有四個彙總旗標

兩個 estimator（及 R8 的 `cortex stat --calibration` 顯示介面）SHALL 採**即時查詢**模式
（每次呼叫時掃描 `evidence/completion/*.json` 與 job 記錄現場計算），不落地為背景批次腳本、
不引入快取或另一份彙總持久化檔案。

理由：`cortex stat` 現有 `--retry-classifications`／`--decomposition-depths`／
`--combo-selections`／`--usage-by-run`（`cli.py:109-137`）四個旗標全部是這個模式
（`aggregate_usage_by_run` 等函式吃 `list[dict]` 現況資料現場算，見
`usage_aggregate.py:15-31`）。沿用既有模式避免引入「快取何時失效」這個新狀態管理問題，且
`CompletionRecord` 檔案量隨自身派工量成長但屬本機檔案系統掃描，現況規模下即時計算成本可
接受。

風險與緩解：若 cortex 派工量成長到即時掃描變慢，`cortex stat` 的既有四個旗標會先出現同樣
的效能問題（不是本票獨有）——緩解：本票不預先優化，若真的發生，屆時應該是一張影響全部五個
彙總旗標的效能票，不是 `#210` 專屬問題，本節僅記錄這是共用的既有取捨，不重新開一套機制。

### D6 `consistency_scope` 維持 `#209` 已凍結的產物種類集合語意，issue §2.3 的 glob 建議不採納

issue §2.3 建議把 `consistency_scope` 寫成 glob 並與 builder persona `write_paths` 比對，
但 `#209` R2 已把 `consistency_scope` 定案為八值枚舉集合（產物**種類**：`code`／`test`／
`spec`／`openspec`／`changelog`／`docs`／`pr`／`issue`），不是路徑 glob。本票裁定：維持
`#209` 已凍結的契約，不因為 issue #210 原文的建議而回頭推翻——`#209` 的凍結早於 `#210`
（`depends_on: [209]`），且 issue #210 本身也是設計討論記錄，不具備片面修改 `#209` 已定案
契約的權限。

理由：issue 原文在 §2.3 標題寫「glob 契約」但 §8.2（`#209` 原文引用處）定義的
`consistency_scope` 值域其實是產物種類列舉，issue 自己就有這個內部不一致；`#209` 定案時已
選了種類列舉這條路（見 `#209` design.md D2／R2 的複合鍵與型別裁決），本票不重新開放這個
已收斂的討論。若「builder `write_paths` 與 glob 比對」這件事仍有必要，SHALL 是一個獨立於
`consistency_scope` 之外的機制（例如既有 `#118` `write_paths` 契約本身的強化），本票不
定案是否需要，留給後續視需要另開票。

理由記錄而非直接改 `#209`：`#209` 已進入 `accepted` 狀態（見其 frontmatter），修改已
`accepted` 的設計文件契約需要獨立票走完整的變更流程，不應該被 `#210` 這張下游設計票夾帶
覆蓋。

## 交付順序（對齊 spec R9）

```
#209 欄位 schema PR（model-identities.yaml v2→v3，四欄位落地＋補至少一個 build 身分）
        │
        ├──> #210 前置票 A：CompletionRecord 新增 plan_invariant_count（或等效欄位，D3）
        │           │
        │           └──> invariant_ceiling estimator（依 D3／D4／R6）
        │
        └──> #210 前置票 B：model-identities.yaml 新增 calibration_source／
             calibrated_at（掛在 invariant_ceiling 上，D1）
                    │
                    └──> 難度後驗 estimator（依 D2／R6，與前置票 A 平行，互不相依）
                                │
                                └──> cortex stat --calibration（R8，至少一個 estimator
                                     可用即可先行串接）
```

## 風險與緩解（彙總）

- **`invariant_count` 從未持久化（D3）是本票發現的最大缺口**：若後續實作票低估這件事的
  工作量（誤以為只是「加一個 estimator」），排程會系統性樂觀——緩解：spec R9／本節交付
  順序圖已明確把「補欄位」列為獨立前置票，不與 estimator 票合併估點。
- **難度後驗 estimator 資料源選錯（D2）**：若實作者直覺沿用 `sizing_declaration_drift`
  （畢竟註解寫著「供 #210 後驗」），會產出粒度錯誤的難度尺度——緩解：spec R2／本節 D2 明文
  記錄正確資料源與排除理由，供 code review 逐條核對。
- **`calibration_source` 被誤掛到全部四個 `#209` 欄位（D1）**：會產生語意錯位的
  policy-vs-observation 混淆——緩解：spec R1／本節 D1 給出「有沒有具體校準演算法」的判準，
  非本票新增欄位時也可比照援引。
- **`consistency_scope` 被 issue 原文的 glob 建議帶偏（D6）**：若實作票直接照抄 issue §2.3
  字面會與 `#209` 已凍結契約衝突，造成兩份設計文件互相矛盾——緩解：本節與 spec R7 明文裁定
  维持 `#209` 契約，並記錄這是本票的裁決而非疏漏。
- **`cortex stat --calibration` 與既有四旗標的規模效能取捨（D5）**：現況規模可接受，成長
  後需要重新評估——緩解：本節記錄為共用取捨，非 `#210` 獨有負擔，避免未來被誤判為本票
  設計缺陷。
