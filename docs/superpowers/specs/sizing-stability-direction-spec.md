---
status: accepted
work_item: sizing-stability-direction
---

# Spec stability 風險方向修正

## Requirements

對應 [Cortex #831](https://github.com/hamanpaul/paulsha-cortex/issues/831)；上位量表見 [#208](https://github.com/hamanpaul/paulsha-cortex/issues/208)，機械計分實作來源 #221。

1. **R1 正向風險**：完整且 accepted、無 blocking marker、deterministic completeness通過的三件組，stability risk=0；增加缺漏或未決變更不得降低風險。
2. **R2 明確映射**：完整三件組=0；恰一個缺失kind且無其他拒收/阻塞=1；至少兩個缺失kind、任一blocking marker，或存在不被accepted的artifact造成authority需重整=2。所有三種kind均無材料屬unknown，不得單憑缺材料宣稱低風險；直接純函式可給保守2，既有claim helper仍回sizing unavailable。
3. **R3 範圍/門檻不變**：每維0..2、總分0..10、Green/Yellow/Red邊界不變；domain/state宣告、acceptance_surfaces與orchestration算法完全不改。
4. **R4 Unknown邊界**：缺少/invalid domain或state在純函式仍ValueError，在既有current_sizing_snapshot仍fail-soft `(None,None)`；不造0、不新增「清欄位繞gate」途徑。未accepted的完整kind集合不得因missing_kinds為0而被當低風險。
5. **R5 新舊版本隔離**：新claim/reclaim及既有明示retry重算入口採新映射；讀取/重啟不得自動重算或改寫歷史run、frozen planning、CompletionRecord或immutable evidence。既有紀錄沒有算法來源時標legacy/unversioned，不猜填新算法。
6. **R6 最小純函式修正**：production只改`coordinator/planning.py`的stability映射及可識別的算法常數；不新增durable欄位、不修改registry loader/CAS或自動migration。若實作需要擴張上述邊界，先重新規劃/計分，不在本票暗中擴張。
7. **R7 可驗證交付**：rubric-based RED/GREEN、monotonicity/property、不同scope與band邊界、claim/retry重算及歷史不改寫測試；全套、policy、CLI read-only smoke與新runtime身份證據分開記錄，不消耗模型測量分數。

## Evidence

基底 `79ba644780bf1c697c722ac24a297e7d02416100` 的 `planning.py:853-859` 用 `max(0, 2 - missing_penalty - blocking_penalty)`，完整accepted給2。
`tests/test_planning_sizing_score.py:62` 固化此反向oracle。
#208原量表的Spec stability明定「frozen且機械驗證完成=0、少量明確缺口=1、authority很可能需改寫=2」；本文件R2是機械映射的顯式delta，不靜默沿用錯誤oracle。

## Non-goals

不改#822 parser、#830派工結果契約、#223自動拆分、模型資格/effort/quota；不改另外兩個機械維度或band門檻，也不把修正後分數回填成歷史當時分數。
