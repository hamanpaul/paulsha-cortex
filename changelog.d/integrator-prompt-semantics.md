---
type: fix
scope: coordinator
---
**Issue #516：integrator prompt 補上兩個 echo-back 欄位的值來源**

`coordinator/planning.py` 的 `_validate_primary_integration()` 要求 integrator
輸出的 `question_pack_id` 與 `secondary_evidence_hash` 與輸入完全相符：

```python
if payload.get("question_pack_id") != question_pack.pack_id:
    raise ValueError("primary integration pack mismatch")
if payload.get("secondary_evidence_hash") != secondary_evidence_hash:
    raise ValueError("primary integration evidence hash mismatch")
```

兩個值本來就都在模型輸入裡——`pack.to_dict()` 帶 `pack_id`，
`callback_payload = {**secondary_payload, "evidence_hash": evidence_hash}` 帶
`evidence_hash`——模型只需**原樣複製**，不需要也不可能自己算出正確的 hash
（`_hash_payload()` 對的是 canonical 化後的 `secondary_payload`）。但
`coordinator/planning_runtime.py` 的 integrator prompt 只把這兩個欄位當欄位名
列在輸出鍵清單裡，沒說值從哪來；更關鍵的是**輸入欄位名（`evidence_hash`）與
輸出欄位名（`secondary_evidence_hash`）不同**，後者字面上像是在要求模型計算
「secondary evidence 的 hash」。模型因此反覆自行算 hash，planning 每次都以
`primary-integration-malformed: ValueError: primary integration evidence hash mismatch`
落 needs_human，Phase 1 派工死鎖。

prompt 現在明確寫出兩者的來源，並直接禁止自算：

> `question_pack_id` must be copied verbatim from the input `question_pack.pack_id`
> value. `secondary_evidence_hash` must be copied verbatim from the input
> `secondary_evidence.evidence_hash` field; do not compute, derive, or invent a hash.

這是 `#406` 註解已記載的同一個教訓（「只列欄位名、不給語意時模型會猜錯」）的第二輪
——當時只補了 `artifact_refs`，兩個 echo-back 欄位漏掉。該註解旁一併補記本次補齊的
是哪兩個欄位。

只改 prompt 文字與對應測試，validator 邏輯與資料流不動。同批盤點過 integrator
prompt 其餘欄位，未再發現同類（會確定性導致驗證失敗的）缺口：`schema_version`
已直接給值，`resolutions[].question_id`／`artifact_kind`／`artifact_refs` 與
`artifacts[].path` 於 `#406` already 補齊語意。`resolutions[].decision` 確實未給
內容期待，但 validator 只要求非空字串（`decision.strip()`），不構成同一失敗模式，
本次不動以維持最小 diff。

不在範圍（後續票）：把 echo-back 值改由呼叫端填入、不交給模型複製（#516 建議 4 的
架構改法）；其他 planning 失敗模式（#507／#511／#514／#515）。
