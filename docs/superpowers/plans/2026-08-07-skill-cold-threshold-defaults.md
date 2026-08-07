---
status: draft
work_item: skill-usage-ledger-park-janitor
---

# cold-skill 判定閾值：初始預設值與待核決事項（issue #204）

issue #204 本文明確要求「定義 cold-skill 判定所需的觀測窗、最低樣本與安全
閾值」，但只給範圍，不給具體數字——這是設計決策而非程式碼技術決策，不應
隱性寫死在常數裡就當作定案。本文件記錄實作時選用的**初始**預設值與理由，
供人類事後核可／調整；調整方式是改
`paulsha_cortex/coordinator/skill_janitor.py` 的
`DEFAULT_MIN_SAMPLES` / `DEFAULT_OBSERVATION_WINDOW_DAYS` 兩個模組常數，或
呼叫端（CLI／未來排程）逐次以參數覆寫，兩個函式（`find_cold_skills`、
`run_janitor_tick`）都不讀死常數，只吃參數。

## 目前預設值

| 參數 | 預設值 | 理由 |
| --- | --- | --- |
| `DEFAULT_MIN_SAMPLES` | 5 | terminal 執行次數 < 5 視為「尚未累積足夠證據」，避免新卡片上線後第一次被跳過／單次執行剛好落在觀測窗邊界就被誤判冷門。 |
| `DEFAULT_OBSERVATION_WINDOW_DAYS` | 30 | 與既有 `manager_daemon.RECENT_DONE_WINDOW_SECONDS`（24 小時，操作面板可見範圍）刻意不同尺度——skill 治理是較低頻的判斷，30 天約可涵蓋一次月度工作週期內至少一次呼叫的合理預期。 |

## 判定邏輯（`find_cold_skills`）

一張 card 同時滿足下列三項才判定為 cold：

1. `card_class` 不是 `core`／`emergency`（永久豁免，連候選資格都沒有）。
2. 該 card 在 ledger 的 `sample_count >= min_samples`（樣本不足＝證據不足，
   不判定，不論方向）。
3. `last_used_at` 早於 `now - observation_window_days`（觀測窗內仍有使用
   紀錄則不算冷；`last_used_at` 缺失一律視為證據不足）。

## 未核決事項（待人類明確核可）

- 目前兩個數字（5 次 / 30 天）未經過任何真實使用資料校準，純粹是「先有一
  個保守、可運作的預設值，之後再依實測資料調整」的起手式。
- `park` 目前只把 skill 記錄進 `skill_park_state_path()`，尚未接上任何
  active router／card 選擇面（issue 非目標區列出的 outcome scoring #137、
  cost routing #138 未觸及；deck combo 選牌路徑目前也沒有讀取 park 狀態的
  消費點）。何時、由誰（很可能是 #139 shared ledger/read model）接上這條
  「park 狀態 → 選牌時排除」的路徑，需要另外決策。
- outcome 目前只有 `completed`／`failed`／`cancelled` 三值；coordinator
  registry 目前的 job status 只有 `exited`／`failed` 兩種 terminal 值，沒有
  獨立的「取消」狀態（見 `paulsha_cortex/coordinator/registry.py` 的
  `TERMINAL_JOB_STATUSES`）。`skill_ledger.derive_outcome` 已預留
  `status == "cancelled"` 分支，一旦 registry 補上該狀態即可零改動生效；
  在那之前，`cancelled` outcome 只能由呼叫端顯式標記產生。
