---
status: accepted
work_item: design-task-type-taxonomy-v2
---

# design-task-type-taxonomy Design

## Decisions

### D1 主軸採 conventional-commit `type`，機械判定

`task_type` 主軸為 `feat`／`fix`／`docs`／`test`／`ci`／`refactor` 六值，由 issue 標題 prefix 機械解析取得，不依賴模型推理、不需人工貼標。

理由：使用者已於 2026-07-27 在 issue #139 裁決此方向。標題 prefix 解析已是 repo 習慣，機械判定滿足「一套分類三處共用」的可重現性要求；且 `type` 與 deck workflow-shape 正交——把 combo 當成 `type × scope` 的輸出而非輸入，#139 與 #202 的循環等待即解開。

### D2 契約檔與程式凍結常數雙鎖

值域同時存在於 `paulsha_cortex/deck/data/task-types.yaml`（資料，含描述與 combo 映射）與 `paulsha_cortex/deck/task_types.py` 的凍結常數（程式契約）；loader 驗證兩者一致，不一致即拒載。

理由：只放 YAML 會讓值域被一次資料 PR 悄悄改掉；只放程式會讓描述與 combo 映射失去資料化彈性。雙鎖使「改值域」必然是兩處同動的顯式決策，而描述與 combo 映射仍可走 data-only PR。骨架落 deck 套件並沿用 `schema.py` 的 `DeckSchemaError` fail-closed 慣例，不另造錯誤體系。

### D3 combo 為輸出投影，缺口以 null 明示

每個 type 的 `combo` 欄位是「分類結果投影到哪個 combo」的映射；現況只有 `feat` → `feature-oneshot`，其餘五值為 null。不在本票新造 combo。

理由：實測最大宗的 `fix`（24/68）沒有 combo 是既成事實；用 null 明示缺口，讓 #202 的 additive-with-fallback 能機械判定「無映射 → 可觀測 bypass」，而不是讓 selector 猜一個最像的 combo。缺口補齊（如 `fix-standard`）屬叢集另案，與 taxonomy 定案解耦。

風險與緩解：null 可能被下游誤當「可自行猜測」——契約明文 MUST bypass，且測試鎖死處置映射。

### D4 分類五類、判準＝「是否明確主張 taxonomy 語彙」

分類結果為 `matched`／`unknown_type`／`ambiguous`／`absent`／`unparseable` 五類；`unknown_type` 與 `ambiguous` 處置為 fail-closed，`absent` 與 `unparseable` 處置為 bypass。

理由：叢集已定案「`ambiguous` fail closed；`absent` 與 `unparseable` bypass 落回明示路徑且須可觀測」。`unknown_type`（如 `perf(cli):`）是「明確主張了一個值域外的 type」——多半是錯字或詞彙漂移，靜默 bypass 會把它藏起來，fail-closed 讓錯誤立刻可見。判準統一為「有主張而不合法 → fail-closed；沒有主張 → bypass」，五類皆有唯一處置，無灰帶。

### D5 受控詞典外的 scope 歸 `ambiguous`、fail-closed

標題 type 合法但 scope 不在受控詞典（例：`fix(claimx): ...`）判為 `ambiguous`，處置 fail-closed；scope 缺省（`fix: ...`）則為合法 `matched`。

理由：#139 的責任是「凍結 scope 受控詞典」；詞典外的 scope 是一個無法對映到既有元件軸的主張，靜默放行會讓 `(type, scope)` 計分鍵（#137／#138／#204 共用）被未受控值污染。緩解：詞典擴充是改 `task-types.yaml` 的 data-only PR，成本低；fail-closed 使詞典缺口立即可見而非累積漂移。

### D6 分類 helper 落在 #139，selector 行為留在 #202

標題解析與五類判定的 helper 由本票落地於 `paulsha_cortex/deck/task_types.py`；「依分類結果選 combo、發 bypass 事件、落回明示路徑」的行為屬 #202。

理由：#202／#137／#138／#204 都需要同一套判定，若各自實作解析必然漂移——這正是 #139 存在的原因。helper 只回傳分類與處置，不做任何 dispatch 決策，邊界乾淨可測。

### D7 log reader 與 status view 只凍結介面契約

R7 的統一 log reader（三合一、64MB/mtime 邊界）與 resource status view（quota＋rate＋health＋track-record 的 JOIN）在本票只定欄位契約，不實作。

理由：本票是 design 票，deliverable 是定案文件＋輕量骨架；reader 與 view 的實作各自是獨立可派工的實作票。先凍結欄位契約讓 #137／#138 可據以並行設計而不互等。緩解「契約與未來實作不符」：實作票只允許 additive 增欄，既有欄位語意凍結。

## 風險與緩解

- **雙鎖兩處漂移**：YAML 與程式常數不一致——loader fail-closed 拒載，任何一處單獨改動都會使全套測試立即變紅。
- **scope 詞典過緊**：合法新元件的標題被判 `ambiguous`——詞典擴充為 data-only PR；fail-closed 的可見性本身就是詞典維護的觸發訊號。
- **legacy `task_type` 欄位撞名混淆**（combo 檔的 `feature`／`mcu-feature` vs taxonomy 的 `feat`）：spec 明載 legacy 欄位為 workflow-shape 標籤、不屬值域；本票不改該欄位，測試不把兩者交叉驗證。
- **下游繞過 loader 自建值域**：R1 明文 MUST NOT；後續 #202／#137／#138／#204 的 review checklist 以「是否 import `task_types`」為機械檢查點。
- **介面契約草案與實作票衝突**：additive-only 擴充規則寫入 R7；欄位語意凍結，變更需回到 #139 修約。
