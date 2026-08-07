---
status: accepted
work_item: design-model-capability-envelope
---

# design-model-capability-envelope Design

## Decisions

- `capable(resource, work)` 定案為六項合取式（AND，不做加權優化）：sizing band（`#208`
  已落地）、invariant 上限、一致性半徑、驗收模式（後三者本票新定義，但前兩者的 work 側資料
  已被 `planning.py:462-472` 消費）、capability 子集（`#130` 已落地）、track record
  （`#137` 只固定簽章，不假設實作）。
- 四個新靜態欄位（`accepts_bands`／`invariant_ceiling`／`consistency_scope`／
  `acceptance_modes`）掛在 `(executor, model_id)` 複合鍵上，與 `model_identities.py` 既有
  去重鍵一致；短期落地於既有 `model-identities.yaml`（schema v2→v3），不新建 issue 原文
  提及但 `#139` 任務清單未涵蓋、repo 內不存在的 `resource-inventory.yaml`。
- 三閘序（eligibility 擋／admission 排隊不擋／routing 選資源）沿用 issue §9.5 既定裁決；
  本票新增記錄一項落差：`claim_readiness.py:57-64` 的 `CHECK_ORDER` 目前把六項檢查放在同一條
  terminal/retryable 二分類交易裡，尚未區分「擋」與「排隊」兩種結局，留給後續實作票處理。
- topic×band 矩陣在 registry 僅 1 個 `build` capability 身分的現況下，只有 eligibility
  語意（該不該派），沒有 routing 語意（派給誰）——分母是 1。
- **現況更正**：`model-identities.yaml`（packaged registry，唯一有 loader 的身分清單）
  全文只有一個身分，`capabilities: [planning]`；issue #209 自身 2026-07-27 修正 comment
  宣稱的三身分表（含 `claude-sonnet-4-6`／`gemini-3.6-flash-high`）與此不符。但這兩個
  model_id 在全 repo grep **並非零命中**：`docs/superpowers/workstreams/
  cost-governance-cluster/todo.md:129` 這份受版控的「關鍵事實」筆記重申幾乎逐字相同的
  三身分表，`driving-cortex-skill/todo.md:12` 另有 1 處提及；`tests/test_model_identities.py`
  1 處為 fixture 字面巧合，與 registry 宣告無關。本文件以 packaged registry（唯一有 loader、
  被程式實際讀取）現況為準，但 todo.md:129 的矛盾尚未收斂，待與其 owner 對齊——詳見
  `docs/superpowers/specs/design-model-capability-envelope-design.md` D6。
- 既有 `planning.py:456-509` 的 `envelope_lookup` provider 介面（`Mapping` 含
  `invariant_count`／`artifact_classes` 兩鍵）必須被後續實作沿用，MUST NOT 另開一條查表
  路徑；`acceptance_mode`（本票新欄位）與既有 `acceptance_surfaces`（`#208`/`#221` 已落地，
  型別/語意皆不同）明確消歧，不合併不改名。

詳細 D1–D9 與風險緩解見
`docs/superpowers/specs/design-model-capability-envelope-design.md`。
