---
status: accepted
work_item: sizing-stability-direction
---

# Spec stability 風險映射設計

## Decisions

### D1 將穩定度訊號轉成風險分，不更動其他維度

在`planning.py`抽出純helper或等價的局部判定；`compute_sizing_score`只替換stability分量。
優先序：任何blocking marker→2；任何assessment遭拒且不屬「單純缺少一種材料」→2；缺失kind至少兩種→2；缺一種→1；其餘完整accepted→0。
同一kind的額外不合格artifact不能被另一份accepted同kind覆蓋而消失。
實作須測試actual assessments與missing_kinds的一致性；不允許以手拼不可能的report使unknown變0。
本映射命名為 `stability-risk-v2`，透過模組常數/文件與精確runtime revision辨識；不新增WorkflowRun或CompletionRecord欄位。

### D2 保留三層 unknown 與失敗邊界

1. Artifact assessment仍負責status/heading/blocking判定；本票不放寬accepted規則。
2. `compute_sizing_score`仍要求合法plan與domain/state宣告；非法值仍拋ValueError。具合法plan但其他kind缺失只輸出有界風險，不代表已達build readiness。
3. `work_bridge.current_sizing_snapshot`既有fail-soft不改：找不到plan/檔案、宣告非法、combo無法解析時仍 `(None,None)`，不得用0替代。

### D3 Frozen與版本遷移採明示重新評估

此次是新計算算法切換，不是資料庫migration。舊run/CompletionRecord已記分數照原樣保存；服務啟動、read model讀取、報告重播不得重算覆寫它們。
新claim/reclaim和已存在的正式retry重算入口，透過既有函式接線取得新分數；重新計分前後的run/attempt與runtime revision由獨立測試/交付證據記錄，不改舊evidence bytes。
若舊紀錄能由歷史部署/原始artifact證據定位算法，就保留其來源；沒有來源則在說明中明示legacy/unversioned，不以今天runtime猜過去算法。
本票不增加新的live全量重算動詞，不修改已有frozen planning metadata後resume。需要採新規劃的工作由正式abandon/reclaim等既有入口建立新世代。

### D4 驗收以規格與性質為oracle

更新既有反向測試之前，先新增完整→0的RED、缺漏/marker增加不降分的property matrix。
保留每維/總分/band邊界，覆蓋Green/Yellow/Red的跨界效果；比較時domain/state/其他機械維度固定，不以改輸入湊分。
對claim helper/正式retry writer/registry roundtrip分別測試：新計算有效、舊記錄未被讀取副作用更寫。測試state僅在tmp_path，不讀live registry。

### D5 範圍與sizing依據

Production單一`planning.py`純函式，domain_breadth=0、state_consistency=0；tests雖涉及callers與registry fixture，不修改production caller/schema。
這不是以移除測試降低scope。若要新增per-run算法欄位、版本migration writer或跨模組修復，已超出本純函式邊界，必須先重拆/重評。
基底舊算法完整標準lane仍6/yellow；使用實際runtime的Yellow機械gate驗證進件，不依預期修後算法偷降現在的分數。
