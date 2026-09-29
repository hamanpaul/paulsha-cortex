### Fixed

- **#1196 quota dispatch demand 的比例單位門檻**：agy 以 0–1 比例回報剩餘額度，舊的 `dispatch-unit:v1` 一律要求 1 個原生單位，等於要求 agy 池滿額；比例單位改以 0.01（1%）為門檻，其餘單位（codex 百分比、request、token）維持 1 個原生單位，receipt 的 demand 版本升為 `dispatch-unit:v2`（#1196）。
