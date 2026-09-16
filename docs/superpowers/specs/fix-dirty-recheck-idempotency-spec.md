---
status: accepted
work_item: fix-dirty-recheck-idempotency
---

# Dirty recheck transition idempotency

## Requirements

Authority：[#496](https://github.com/hamanpaul/paulsha-cortex/issues/496)，保留 [#832 已接受 todo](https://github.com/hamanpaul/paulsha-cortex/blob/60a3ffa867377b0c86fa2f10e91fb9820d91938c/docs/superpowers/workstreams/fix-dirty-recheck-idempotency/todo.md) 的完整修正。accepted 是進件內容定案，不是實作、測試或 runtime 驗收完成。

1. **D01 Validated recheck**：保留 needs_human dirty-summary 的定期 verification；只在 `complete_tick` 的 dirty recheck 呼叫點，經 `_validate_result_evidence` 後決定是否呼叫 `_apply_verification_result`，不移除 recheck 或改變其他呼叫者。
2. **D02 Exact equality**：canonical content hash 與 `current_verification_evidence_hash`、effective state/gate_state、candidate、summary、current evidence refs 全部一致才 no-op。同路徑不是同內容，缺合法 hash／無法解析 current evidence 不能當相等。
3. **D03 No-op writes**：結果未變不 `record_action`、不追加 evidence_history、不 `update_slice`；runner 仍確實執行。Optional last-rechecked telemetry 不進 lifecycle history，不成為每 tick 寫入的替代來源。
4. **D04 Real transitions**：hash/status/gate/candidate/summary/refs 任一真實變更只記一筆 action/history，current evidence hash 更新；相同內容但 state/ref 需修復也算一次轉換。下一相同結果為 no-op；contract verification hash 不改。
5. **D05 Fail closed**：新 evidence 不可讀、schema 錯誤或 payload mismatch，維持既有保守錯誤處理，不放行、不空值相等、不 catch-all 轉成成功。
6. **D06 Stable polling test**：隔離 fixture 連續 10 個 complete ticks，同候選／內容，runner count 持續增加，actions/history 相對既有基準不增加；修前須可重現 RED，不能調低 history limit 偽造成功。
7. **D07 Content negative**：第 K 次在受控 fixture 保持 evidence path 但改 details/canonical hash，恰好追加一次，後續相同 tick 不追加。這是測試驗 equality，不授權 production writer 覆寫 immutable evidence。
8. **D08 Repair and cleanup**：dirty→verified、candidate 改變、gate_state/ref 不一致修復各恰好一次；保留既有 dirty cleanup 會脫困的 regression，不依賴 #497/#821 已交付。
9. **D09 Hash compatibility**：壞檔／不可讀／payload mismatch 負例與 #501 normalization 回歸通過；不得使用或改寫 `slice.verification.hash` 作為 current evidence hash，不重寫 #501 migration。
10. **D10 Delivery evidence**：focused RED/GREEN、必要全套與 CI/policy、獨立 review、exact-head merge、loaded runtime 重複 tick 的 action/history delta 分開留證。`changelog.d/fix-dirty-recheck-idempotency.md` 與 CHANGELOG 同步；純進件不勾產品完成。

## Evidence

Production/source 基底 `79ba644780bf1c697c722ac24a297e7d02416100`，與 intake 基底 `60a3ffa8` 在相關 code/tests 無差異。

- [manager.py:2093](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L2093-L2129)：dirty summary 觸發 runner，驗證後無條件 apply。
- [manager.py:394](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L394-L449)：validator 核 payload/hash/實體檔，apply 呼叫 record_action 再 update_slice。
- [registry.py:1591](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L1591-L1676)：真正追加 history/action 的是 record_action，不是 update_slice。
- [existing cleanup fixture](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/tests/test_pre_candidate_recovery.py#L241-L330)：證明 dirty recheck 可轉到新 candidate，尚不是 10 次未變 no-op 證據。

## Non-goals

不改 #497 attempt supersession、#821 persist/history、#501 bucket index authority、immutable writer/quarantine、fanout、tick clock、evidence 位址或 verification runner 能力。不新增全域去重引擎，也不把歷史 116 秒 33 筆當本次 live 量測。
