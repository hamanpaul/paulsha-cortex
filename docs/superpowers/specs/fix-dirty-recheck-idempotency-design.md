---
status: accepted
work_item: fix-dirty-recheck-idempotency
---

# Dirty recheck design

## Decisions

1. **Single caller seam**：production 修改限 `coordinator/manager.py` dirty recheck 與必要純比較 helper；既有 registry／verification 是不改動的消費契約。domain_breadth=0（單 production 模組／單資料流），不能把測試消費端灌成新 production 領域。
2. **Validate before equality**：新結果先過現行 `_validate_result_evidence`。再重新取得 current slice 與目前合法 evidence，組成 effective transition tuple：canonical hash、status、依既有規則推導的 gate_state、candidate、summary、refs。驗證失敗沿原錯誤路徑；不能把 unknown/null tuple 當相等。
3. **No-op before apply**：只有 tuple 相等才略過 `_apply_verification_result`。其餘沿現行 apply 更新，避免改原語其他呼叫者的語意。state_consistency=1：涉及 lifecycle state/hash/refs 的一致性判斷，但本票不建立跨物件交易或改 writer。
4. **Transition semantics**：reviewing→pending gate、verified→passed gate、其他合法結果→needs_human，與既有 apply 對齊；summary 從驗過的 evidence payload 取得，不新增另一份 summary authority。內容相同但 state/ref 壞掉必須修復一次。
5. **Current hash is not contract hash**：讀 `current_verification_evidence_hash`；保持 #501 的既有 normalized legacy row、contract/evidence hash 分離與 immutable evidence 規則。比較 helper 不清洗或回填無法證明的 hash。
6. **Isolated negative fixtures**：使用本機 fixture repo、明確 repo root 與假的可計數 verification runner。D07 同 path 不同內容只在專用隔離 fixture 建置；不得改 production writer 讓內容碰撞可以覆寫。
7. **Tick isolation**：測試需預置合法 current terminal/manifest 關係，使測到的是 dirty recheck，不被另一條 terminal replay 干擾；另保留原 cleanup 測試與 current attempt 正常 completion 回歸，不能用 skip runner 使 no-op 假綠。
8. **Recovery boundary**：本票不宣稱 record_action＋update_slice 已成新原子交易；CAS／durable attempt supersession 由 #497 原責任處理。未變結果不寫入、真實轉換恰好一次的正常 tick 契約先獨立驗證，跨程序 writer 保證仍受 #818 限制。
9. **Completion boundary**：spec D01–D10 對應 todo T01–T10，T11–T12 補 documentation/CLI 與純計分要求。正式 Cortex intake/identity/review/loaded-runtime 仍由主流程處理，不拿此 accepted 文件當已採信 runtime evidence。

## Verification

新 focused 檔建議 `tests/test_dirty_recheck_idempotency_496.py`。斷言 runner 呼叫數、record_action/update_slice 呼叫數、history delta、current hash、contract hash、state/refs；對 D05/D09 不只檢查函式沒拋例外，還檢查沒有取得成功狀態／新的 accepted evidence。保留 `tests/test_coordinator_registry_headless.py` 的 legacy hash normalization。

## Compatibility and Risks

無新 CLI action、無 schema／evidence-address migration。CLI help 只做真實唯讀 regression。Complete tuple 比較的正確性由逐欄負例守門，不以「掃不到其他路徑」當全稱背書。當前算法會把完整 spec stability 算 2；#831 定案後應為 0，兩種純計算結果寫入 intake report，不回填歷史 run。
