---
type: fix
scope: coordinator
---
**Issue #554：taxonomy marker 無詞界，與 #543 的 `<unavailable>` 佔位符相撞；順帶收掉 worktree drift 的 `content` 誤分類死鎖**

兩個缺陷共用同一組現場（planning worktree drift 的失敗訊息），一併處理。

### 缺陷一：`<unavailable>` 佔位符誤中 transient marker

`planning_runtime._operator_drift_message` 的尾端是 `evidence={location}`，
`location = report_path or backup_root or "<unavailable>"`；而 PR #542 落地的
`outcome_taxonomy.TRANSIENT_SERVICE_MARKERS` 含**裸** `"unavailable"`（#533 為 agy
的 `UNAVAILABLE (code 503)` 而收），比對是無界子字串。因此 drift **且備份／報告
雙雙寫入失敗**時，一個純環境事件會被 `matches_transient_service_markers` 判成
transient-service：

- `evidence=/tmp/psc-report.json` → False（正常）
- `evidence=<unavailable>` → True（誤判，靠子字串巧合）

這是 #500（`\btimeout\b` 命中 nested tool result 的 `Parser aborted (timeout, ...)`）、
#487（`oauth` 命中技能名 `doc-coauthoring`）的同族無界 token 缺陷，第三次命中。

**兩邊都修，缺一不可。**

1. **marker 比對加詞界**：新增 `outcome_taxonomy.TRANSIENT_SERVICE_MARKER_RE`
   （`\b(?:…)\b`，IGNORECASE），`matches_transient_service_markers` 改用它。這擋住
   「marker 被埋在更長 word token 裡」的誤中面——全表掃描的結果是裸短字串
   `"503"`／`"429"` 誤中面最大（run id、content-addressed digest、evidence 路徑裡
   出現三個數字是家常便飯，例如 `workflow-1a503f0429ab` 修法前就會被判 transient），
   `"unavailable"` 次之（`envelope_unavailable`、`provider_unavailable` 這類與服務層
   無關的內部欄位值）。`INTERRUPTION_MARKERS`／`KNOWN_PROCESS_BANNERS`／
   `_PLANNING_AUTHORITY_RESIDUE_MARKERS` 一併掃過：全是長片語或 snake_case 整串
   token，沒有同型誤中面，不動。
2. **佔位符不得含 marker**：詞界擋不住「整個 token 就是 marker」——`<unavailable>`
   的 `<`／`>` 都不是 word char，詞界照樣成立（issue 順手提到的
   `<evidence-unavailable>` 同理，`-` 也不是 word char）。因此新增
   `planning_runtime.PLANNING_WORKTREE_DRIFT_EVIDENCE_PLACEHOLDER = "<not-written>"`
   並附不變式測試，未來換佔位符會被測試擋下。

詞界化的代價是「原本靠子字串巧合命中的真陽性變體」會落空。那些是真訊號，因此改為
**顯式列舉**進表：`rate limits`／`rate limited`／`rate limiting`（provider 訊息實測
三種都出現）、`timeouts`、`timeouterror`／`timeoutexpired`、`serviceunavailable`。
其中 CamelCase 例外類名特別關鍵——planning lane 的 reason 格式是
`<stage>-<kind>: <ExceptionTypeName>: <str(exc)[:160]>`，`subprocess.TimeoutExpired`
的訊息（`Command '[...]' timed out after N seconds`）常因 argv 過長而在 160 字截斷
處被切掉 `timed out`，此時**只剩型別名帶得動訊號**。表變長是刻意的：寧可每個變體都
看得見，也不要再靠子字串巧合。

### 缺陷二：worktree drift 改判 `environment`（operator 裁決）

#507 前 drift 的處置是把 operator worktree 整棵抹除再從 baseline 還原——那確實會
銷毀資料，歸 `content`（fail-closed、不給 `recover-planning`）在當時是合理的保守。
#543 之後處置已改為「一個位元組都不動、只備份與報告」，drift 於是變成純粹的環境
事件：本次 planning 結果不可信，但沒有任何東西被破壞，重跑就好。維持 `content` 讓
唯一出口是 `abandon`（燒一個世代），即 #507 comment 2 記錄、#543 明文留待後續的
死鎖。

新增 `manager._is_planning_worktree_drift_failure`，判準只認 `planning_runtime`
新匯出的**穩定前綴** `PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX`
（`planning launcher modified operator worktree`）——訊息尾段已經在 #543 改過一次
（`changes rolled back` → `operator content preserved`），計數與 evidence 路徑更是
每次都不同，任何依賴尾段字面的判準都會再壞一次。判準刻意窄：同一段 finally 另有的
`planning launcher modified disposable read-only sandbox`（launcher 寫壞拋棄式沙箱，
屬 launcher 行為異常而非環境並行編輯）不在此列，維持 `content`。

同時把 `_run_define_stage` 中段的三元表達式抽成具名的
`manager._classify_planning_failure(reason) -> "environment" | "content"`，讓
「reason → classification」這條映射有單一可測入口（過去它只活在函式中段，測不到也
看不見）；三個 `environment` 例外（#416 authority 殘留、#533 暫時性服務、#554
drift）在同一處列齊。

### 範圍紀律

只動分類與 marker 精度。`recover-planning` 自身的 admission、CAS、fail-closed 條件
一字未改——本次只是讓 drift 案例走得到它。

新增 `tests/test_planning_drift_classification_554.py`（47 個回歸測試：佔位符不變
式、全表詞界不變式的參數化掃描、數字 marker 不再命中 run id／路徑、snake_case 內部
欄位值不再命中、詞界化保留的真陽性變體、drift 新舊兩代訊息都正確分類、sandbox 家族
維持 `content`、`_resume_decision` 對 drift 浮現 `recover-planning`）。
