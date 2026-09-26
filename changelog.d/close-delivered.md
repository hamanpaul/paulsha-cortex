修正 #895：新增 `cortex work close-delivered`，讓 operator 可為沒有 `WorkflowRun` 的管線外交付提供 actor／reason；Manager 驗證既有遠端 closure 後寫入 immutable CompletionRecord，Monitor 沿用原有 strict closure 條件投影狀態。
