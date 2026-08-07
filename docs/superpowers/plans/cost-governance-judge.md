---
status: accepted
work_item: cost-governance-judge
---

# cost-governance-judge Plan

本票（`#138`）交付的是設計文件（`cost-governance-judge-{spec,design}.md`），不寫任何
`.py` 程式碼。本檔案不是本票自己的 TDD 執行計畫，而是比照 `#208` → `#211`–`#223` 的
拆分模式，把設計文件的七個 Requirements 拆成可獨立派工的後續**實作票**清單，供未來
以 OpenSpec change 落地時當作派工前置清單（issue 原文結尾「落地時再依 OpenSpec 流程
開 change」）。

## 依賴前提（派工前必讀）

- `#209 capable()` 本體目前為**已落地設計（`design-model-capability-envelope-
  {spec,design}.md` 在 main）、尚未 code-landed**。`#137 track_record()` 本體現況
  更早一階——**設計本身也尚未落地 main**（`oneshot-lesson-loop-{spec,design}.md`
  只存在於未合併的 `feature/137-oneshot-lesson-loop-design` 分支，詳見
  `cost-governance-judge-design.md`「與 `#137` 狀態的訂正說明」），須先合併設計、
  再落地程式碼。下列子票中標「可獨立先做」的不需要等它們，標「依賴」的需要等對應
  票的程式碼落地（`#137` 一項還額外多一道「先合併設計」的前置）。
- 四個 interim stub（design D6／spec R4）保證骨架先行不會讓現有派工行為退化，因此
  子票 2（`filter_ready` 骨架）可以在子票 1（`rate_tracker`）之前或並行動工，不構成
  阻擋。
- 已落地票 `#325`（job usage schema）與本票子票 1（`rate_tracker.py`）**無直接依賴**：
  `#325` 的 `registry.py` `usage`／`usage_raw` 是歷史 per-job 記錄，子票 1 是即時
  per-resource 速率閘門，兩者資料源不同（詳見 design.md／spec.md「與相鄰票的介面
  關係」）；`#324`（combo 可擴充與可選）與本票任何子票皆無介面耦合，僅記錄查證結果
  供派工參考，不影響本表拆分。

## 後續實作票拆分

### 1. `rate_tracker.py`：token bucket 自追（可獨立先做，零外部前置）

- 落地 `paulsha_cortex/coordinator/rate_tracker.py`：`consume()`／`record_429()`／
  `rate_status()`（spec R1／R3／R7）。
- 驗收：純函式單元測試涵蓋 bucket 惰性補充、429 收縮 `capacity`、冷啟動空桶行為；
  `rate_status()` 回傳形狀與 `RateSnapshot`（spec R1 表格）一致。
- 不依賴 `#137`／`#209`。

### 2. `filter_ready` 骨架：控速分流層介面（可獨立先做，四因子 stub 全恆真）

- 落地 `filter_ready()`（spec R2），掛在 `autonomy.ready_units()` 與
  `dispatch_ready()` 之間的 manager tick 步驟。
- 四因子（`rate_available`／`quota_remaining`／`capable`／`track_record`）依 spec R4
  的 interim stub 表全部先恆真——驗收基準是「掛上此步驟後既有派工行為不變」（no-op
  回歸測試：`filter_ready` 的輸出在四個 stub 恆真時應與輸入 `units` 完全相同，
  `queued` 恆為空）。
- 明確不依賴 `#136` `capacity_gate.py`，不得修改該檔案（design D4 邊界）。

### 3. `rate_available` stub 替換為真值

- 把子票 2 的 `rate_available` stub 換成呼叫子票 1 的 `rate_tracker.consume()`。
- 依賴：子票 1、子票 2 皆完成。

### 4. `capable` stub 替換為真值

- 依賴：`#209` R1 `capable()` 本體 code-landed（跨票依賴，不在本 cluster 掌控範圍）。
- 落地時需先確認 `model-identities.yaml` 是否已補上至少一個具 `build` capability 的
  身分（`design-model-capability-envelope-design.md` D6／風險與緩解已載明此前置檢查）
  ，否則 `capable()` 上線會讓現行 build 派工從「無過濾」變成「全部擋下」。

### 5. `track_record` stub 替換為真值

- 依賴：`#137` R4 `track_record()` 本體 code-landed。

### 6. `quota_remaining` 接線（跨 repo，範圍最大，需先對齊 `paulshaclaw` owner）

- 依賴：`paulshaclaw/cost/` 暴露可供本 repo 消費的 quota view 介面——這不是本 repo
  單方面可以落地的票，需要先有一張與 `paulshaclaw` owner 對齊介面形狀的溝通/設計
  子票，不建議直接開實作票。

### 7. `should_terminate` 觸發框架 + session-health 門檻

- 落地 `should_terminate()`（spec R5）與 `launcher.py` wrapper 執行迴圈的掛勾點。
- session-health 門檻數值需 `#210`（以自身 run 歷史校準）先有基礎資料；stall／報酬
  遞減判準本票明文不預先定義，此票需要自行設計判準（design D7 已標注為「本票範圍內
  唯一全新概念」，風險最高，建議獨立一張票、不與觸發框架落地綁在同一顆 PR）。

### 8.（可選）`resource-inventory` 新增靜態欄位

- 僅在證實 MVP judge 需要 `context_window`／`quota_window_kind`／
  `autonomy_safety_profile` 時才開票；遵循 spec R6 的 additive 擴充路徑，不新建檔案。

## 建議派工順序

第一波可並行：子票 1、子票 2（互不碰檔，且子票 2 不需要等子票 1）。
子票 3 依賴子票 1+2；子票 4／5／6 各自依賴外部票（`#209`／`#137`／`paulshaclaw`）
code-landed，屬長尾追蹤項，不建議現在派工，待前置條件成立後再開票。子票 7 可與第一波
並行（觸發框架部分），但 session-health 門檻數值段落建議延後到 `#210` 有資料後再收斂。
