#578、#555：verify／review 的 malformed terminal 現在沿用既有 per-card schema retry 額度有限重派，達上限後停在 `needs_human`；合法明示停止與有效 envelope 的 gate／authority 採信錯誤不重派，operator `retry-card` 熔斷規則維持不變。
